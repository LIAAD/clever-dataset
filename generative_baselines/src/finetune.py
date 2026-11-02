"""Fine-tuning on one supervision configuration and one-time generation for the CLEVER test set.

Every model follows the recipe of configs/fine_tuning.yaml: the learning rate of its adaptation
family (full fine-tuning below 1B parameters, LoRA from 1B to below 7B, QLoRA from 7B), the
effective batch, the warm-up and the epoch at which training stops, while the linear
learning-rate schedule spans `scheduler_horizon` epochs. Training files:
data/fine_tuning/train_<configuration>.jsonl, written by src/prepare_data.py.

    python src/finetune.py --config configs/models_encdec.yaml --configuration gold91_silver1073
"""

import argparse
import tempfile
import traceback
from pathlib import Path

from experiment_utils import build_prediction_record, prediction_filename, select_models, set_seed
from generate_predictions import generate_for_examples
from io_utils import load_examples, load_yaml, write_jsonl
from model_utils import (fit_input, free_model, load_decoder, load_encoder_decoder,
                         neutral_generation_config, stop_token_overrides, to_device)
from prompts import build_target
from training_protocol import (aplicar_adaptacao, gradient_accumulation_steps,
                               loss_seq2seq_normalizada, make_horizon_trainer_classes)

ROOT = Path(__file__).resolve().parents[1]


def build_seq2seq_dataset(examples, tokenizer, max_source_length, max_target_length):
    """Prompt -> target (one question per line) pairs for encoder-decoder models."""
    from datasets import Dataset

    sources = [fit_input(tokenizer, e, max_source_length) for e in examples]
    targets = [build_target(e["questions"]) for e in examples]

    def tokenize(batch):
        model_inputs = tokenizer(batch["source"], max_length=max_source_length,
                                 truncation=True, padding=False)
        labels = tokenizer(text_target=batch["target"], max_length=max_target_length,
                           truncation=True, padding=False)
        model_inputs["labels"] = labels["input_ids"]
        return model_inputs

    ds = Dataset.from_dict({"source": sources, "target": targets})
    return ds.map(tokenize, batched=True, remove_columns=["source", "target"])


def build_causal_dataset(examples, tokenizer, max_seq_length):
    """prompt + target + EOS per example, with the prompt masked out of the labels."""
    from datasets import Dataset

    eos = tokenizer.eos_token or ""

    def encode(ex):
        answer = build_target(ex["questions"]) + eos
        answer_ids = tokenizer(answer, add_special_tokens=False)["input_ids"]
        prompt = fit_input(tokenizer, ex, max_seq_length, reserve=len(answer_ids))
        prompt_ids = tokenizer(prompt, add_special_tokens=True)["input_ids"]
        input_ids = (prompt_ids + answer_ids)[:max_seq_length]
        labels = ([-100] * len(prompt_ids) + answer_ids)[:max_seq_length]
        return {"input_ids": input_ids, "attention_mask": [1] * len(input_ids), "labels": labels}

    return Dataset.from_list([encode(e) for e in examples])


def make_causal_collator(pad_id):
    import torch

    def collate(features):
        maxlen = max(len(f["input_ids"]) for f in features)
        input_ids, attn, labels = [], [], []
        for f in features:
            pad = maxlen - len(f["input_ids"])
            input_ids.append(f["input_ids"] + [pad_id] * pad)
            attn.append(f["attention_mask"] + [0] * pad)
            labels.append(f["labels"] + [-100] * pad)
        return {
            "input_ids": torch.tensor(input_ids),
            "attention_mask": torch.tensor(attn),
            "labels": torch.tensor(labels),
        }

    return collate


def _args_treino(args, tmp_dir: Path, seq2seq: bool):
    from transformers import Seq2SeqTrainingArguments, TrainingArguments

    grad_accum = gradient_accumulation_steps(args.effective_batch_size, args.batch_size)
    # Same precision for every family: bf16 autocast over fp32 (or NF4) weights.
    comuns = dict(
        output_dir=str(tmp_dir),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=grad_accum,
        learning_rate=args.learning_rate,
        warmup_ratio=args.warmup_ratio,
        bf16=True,
        eval_strategy="no",
        save_strategy="no",
        load_best_model_at_end=False,
        logging_strategy="epoch",
        report_to=[],
        seed=args.seed,
        data_seed=args.seed,
    )
    return Seq2SeqTrainingArguments(**comuns) if seq2seq else TrainingArguments(**comuns)


def treina_encdec(model_cfg, train_examples, test_examples, args, tmp_dir: Path):
    from transformers import DataCollatorForSeq2Seq

    _, HorizonSeq2SeqTrainer = make_horizon_trainer_classes()

    short, arch = model_cfg["short_name"], model_cfg["architecture"]
    max_src = model_cfg.get("max_source_length", args.max_source_length)
    max_tgt = model_cfg.get("max_target_length", args.max_target_length)

    # The adaptation family decides quantisation: only QLoRA loads in 4-bit.
    load_4bit = args.familia_adaptacao == "qlora"

    print(f"\n=== [fine_tuning:{args.configuration}] {short} ({arch}, "
          f"{args.familia_adaptacao}, "
          f"{'pesos NF4 4-bit + compute bf16' if load_4bit else 'pesos fp32 + autocast bf16'}) ===")
    model, tokenizer = load_encoder_decoder(
        model_cfg["model_name_or_path"], args.offline, bf16=load_4bit,
        load_in_4bit=load_4bit)
    model = to_device(model)
    model = aplicar_adaptacao(model, args.familia_adaptacao, r=args.lora_r,
                              alpha=args.lora_alpha, dropout=args.lora_dropout)

    trainer = HorizonSeq2SeqTrainer(
        model=model,
        args=_args_treino(args, tmp_dir, seq2seq=True),
        # Loss normalised over the whole accumulation window (see training_protocol).
        compute_loss_func=loss_seq2seq_normalizada,
        scheduler_horizon_epochs=args.scheduler_horizon_epochs,
        train_dataset=build_seq2seq_dataset(train_examples, tokenizer, max_src, max_tgt),
        data_collator=DataCollatorForSeq2Seq(tokenizer, model=model),
    )
    trainer.train()

    gen_cfg = neutral_generation_config()
    resultados = generate_for_examples(
        trainer.model, tokenizer, test_examples, arch, gen_cfg,
        lambda ex: fit_input(tokenizer, ex, max_src), max_src)
    return trainer, tokenizer, gen_cfg, resultados


def treina_decoder(model_cfg, train_examples, test_examples, args, tmp_dir: Path):
    HorizonTrainer, _ = make_horizon_trainer_classes()

    short, arch = model_cfg["short_name"], model_cfg["architecture"]
    max_seq = model_cfg.get("max_seq_length", args.max_seq_length)
    load_4bit = args.familia_adaptacao == "qlora"

    print(f"\n=== [fine_tuning:{args.configuration}] {short} ({arch}, "
          f"{args.familia_adaptacao}) ===")
    model, tokenizer = load_decoder(
        model_cfg["model_name_or_path"], args.offline, bf16=load_4bit,
        load_in_4bit=load_4bit)
    model = aplicar_adaptacao(model, args.familia_adaptacao, r=args.lora_r,
                              alpha=args.lora_alpha, dropout=args.lora_dropout)

    trainer = HorizonTrainer(
        model=model,
        args=_args_treino(args, tmp_dir, seq2seq=False),
        scheduler_horizon_epochs=args.scheduler_horizon_epochs,
        train_dataset=build_causal_dataset(train_examples, tokenizer, max_seq),
        data_collator=make_causal_collator(tokenizer.pad_token_id or tokenizer.eos_token_id),
    )
    trainer.train()

    gen_cfg = neutral_generation_config()
    gen_cfg.update(stop_token_overrides(model_cfg, tokenizer))
    prompt_fn = lambda ex: fit_input(tokenizer, ex, max_seq, reserve=gen_cfg["max_new_tokens"])
    resultados = generate_for_examples(
        trainer.model, tokenizer, test_examples, arch, gen_cfg, prompt_fn, max_seq)
    return trainer, tokenizer, gen_cfg, resultados


def hiperparametros(model_cfg: dict, receita: dict, configuracao: str) -> dict:
    """The recipe of one model in one supervision configuration."""
    short, familia = model_cfg["short_name"], model_cfg.get("adaptation")
    if familia not in receita["learning_rate"]:
        raise ValueError(f"{short}: adaptation {familia!r} sem learning rate na receita.")
    epocas = receita["epochs"].get(short, {}).get(configuracao)
    if epocas is None:
        raise ValueError(f"{short}: sem epochs para {configuracao} na receita.")
    if epocas["epochs"] > epocas["scheduler_horizon"]:
        raise ValueError(f"{short}: epochs acima do horizonte do scheduler em {configuracao}.")
    return {
        "familia_adaptacao": familia,
        "learning_rate": float(receita["learning_rate"][familia]),
        "epochs": int(epocas["epochs"]),
        "scheduler_horizon_epochs": int(epocas["scheduler_horizon"]),
        "batch_size": int(model_cfg["physical_batch_size"]),
        "effective_batch_size": int(receita["effective_batch_size"]),
        "warmup_ratio": float(receita["warmup_ratio"]),
        "lora_r": int(receita["lora"]["r"]),
        "lora_alpha": int(receita["lora"]["alpha"]),
        "lora_dropout": float(receita["lora"]["dropout"]),
    }


def treina_um(model_cfg, train_examples, test_examples, args, receita) -> None:
    margs = argparse.Namespace(**vars(args), **hiperparametros(model_cfg, receita, args.configuration))
    arch = model_cfg["architecture"]
    fn = treina_encdec if arch == "encoder_decoder" else treina_decoder
    print(f"[receita] lr={margs.learning_rate:g} | epochs={margs.epochs} | "
          f"scheduler horizon={margs.scheduler_horizon_epochs}")
    with tempfile.TemporaryDirectory(prefix="clever_fine_tuning_") as tmp_dir:
        trainer, tokenizer, gen_cfg, resultados = fn(
            model_cfg, train_examples, test_examples, margs, Path(tmp_dir))

    grad_accum = gradient_accumulation_steps(margs.effective_batch_size, margs.batch_size)
    destino = Path(args.predictions_dir) / prediction_filename(
        "fine_tuning", args.configuration, arch, model_cfg["short_name"], args.seed)
    training_meta = {
        "epochs_selected": margs.epochs,
        "scheduler_horizon_epochs": margs.scheduler_horizon_epochs,
        "learning_rate": margs.learning_rate,
        "warmup_ratio": margs.warmup_ratio,
        "physical_batch_size": margs.batch_size,
        "gradient_accumulation_steps": grad_accum,
        "effective_batch_size": margs.effective_batch_size,
        "precision": ("nf4 4-bit + bf16" if margs.familia_adaptacao == "qlora"
                      else "fp32 weights + bf16 autocast"),
        "familia_adaptacao": margs.familia_adaptacao,
        "early_stopping": False,
    }
    if margs.familia_adaptacao == "full_fine_tuning":
        training_meta["adaptation"] = "full fine-tuning"
    else:
        training_meta.update({
            "adaptation": "QLoRA 4-bit" if margs.familia_adaptacao == "qlora" else "LoRA",
            "lora_r": margs.lora_r,
            "lora_alpha": margs.lora_alpha,
            "lora_dropout": margs.lora_dropout,
            "lora_target_modules": "all-linear",
        })

    registos = [
        build_prediction_record(
            ex, perguntas, raw,
            model_cfg=model_cfg,
            regime="fine_tuning",
            train_setting=args.configuration,
            claimpt_percentage=receita["configurations"][args.configuration].get("silver"),
            seed=args.seed,
            generation_config=gen_cfg,
            few_shot_example_ids=[],
            training=training_meta,
        )
        for ex, (perguntas, raw) in zip(test_examples, resultados)
    ]
    write_jsonl(destino, registos)
    print(f"[ok] {len(registos)} predictions -> {destino}")

    free_model(trainer.model, tokenizer, trainer)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", required=True, help="configs/models_encdec.yaml or configs/models_decoder.yaml")
    p.add_argument("--configuration", required=True,
                   help="Supervision configuration of the recipe (e.g. gold91_silver1073).")
    p.add_argument("--recipe", default=ROOT / "configs" / "fine_tuning.yaml")
    p.add_argument("--only", default=None, help="Comma-separated short names.")
    p.add_argument("--train_file", default=None,
                   help="Default: data/fine_tuning/train_<configuration>.jsonl.")
    p.add_argument("--test_file", default="data/clever/clever_test_100.jsonl")
    p.add_argument("--predictions_dir", default="outputs/predictions")
    p.add_argument("--max_source_length", type=int, default=1024)
    p.add_argument("--max_target_length", type=int, default=192)
    p.add_argument("--max_seq_length", type=int, default=2048)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--offline", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    receita = load_yaml(args.recipe)
    if args.configuration not in receita["configurations"]:
        raise SystemExit(f"Configuração desconhecida: {args.configuration}. "
                         f"Disponíveis: {sorted(receita['configurations'])}")
    set_seed(args.seed)
    models = select_models(load_yaml(args.config)["models"], args.only)
    if not models:
        raise SystemExit("Nenhum modelo selecionado.")

    if args.train_file is None:
        args.train_file = f"data/fine_tuning/train_{args.configuration}.jsonl"
    train_examples = load_examples(args.train_file)
    test_examples = load_examples(args.test_file)
    if not train_examples or not test_examples:
        raise SystemExit(f"Sem exemplos em {args.train_file} ou {args.test_file} "
                         f"(correr src/prepare_data.py).")
    print(f"[fine_tuning] configuração={args.configuration} | treino={len(train_examples)} | "
          f"teste={len(test_examples)}")

    for model_cfg in models:
        try:
            treina_um(model_cfg, train_examples, test_examples, args, receita)
        except Exception as e:
            print(f"[ERRO] modelo {model_cfg.get('short_name')} falhou: {e}")
            traceback.print_exc()
        finally:
            free_model()
    print("\n[fine_tuning] concluído.")


if __name__ == "__main__":
    main()
