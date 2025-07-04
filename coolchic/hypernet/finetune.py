import argparse
import gc
from collections import defaultdict
from pathlib import Path
from typing import Literal

import matplotlib.pyplot as plt
import pandas as pd
import torch
import wandb
from torchvision.extension import os

from coolchic.enc.component.coolchic import CoolChicEncoder, CoolChicEncoderParameter
from coolchic.enc.io.format.png import read_png
from coolchic.enc.io.io import load_frame_data_from_file
from coolchic.enc.training.presets import TrainerPhase, Warmup
from coolchic.enc.training.quantizemodel import quantize_model
from coolchic.enc.training.test import test
from coolchic.enc.training.train import train as coolchic_train
from coolchic.enc.utils.misc import get_best_device
from coolchic.enc.utils.parsecli import (
    get_coolchic_param_from_args,
)
from coolchic.eval.hypernet import find_crossing_it, plot_hypernet_rd
from coolchic.eval.results import SummaryEncodingMetrics, log_to_results
from coolchic.hypernet.hypernet import (
    CoolchicWholeNet,
    DeltaWholeNet,
    NOWholeNet,
    WholeNet,
)
from coolchic.hypernet.inference import load_hypernet
from coolchic.utils.coolchic_types import get_coolchic_structs
from coolchic.utils.paths import (
    DATA_DIR,
    DATASET_NAME,
    RESULTS_DIR,
    get_latest_checkpoint,
)
from coolchic.utils.types import (
    DecoderConfig,
    HypernetRunConfig,
    PresetConfig,
    load_config,
)


def finetune_coolchic(
    img_path: Path,
    preset_config: PresetConfig,
    cc_encoder: CoolChicEncoder,
    lmbda: float,
    dec_cfg: DecoderConfig,
) -> list[SummaryEncodingMetrics]:
    # Get image
    frame_data = load_frame_data_from_file(str(img_path), 0)
    img = frame_data.data
    assert isinstance(img, torch.Tensor)  # To make pyright happy.
    device = get_best_device()
    img = img.to(device)

    # Some auxiliary data structures.
    frame, frame_encoder_manager, frame_enc = get_coolchic_structs(
        lmbda, preset_config, dec_cfg, cc_encoder, frame_data
    )
    # Add seq name to frame, useful for logging.
    frame.seq_name = img_path.stem

    # Train like in coolchic
    frame.to_device(device)
    frame_enc.to_device(device)
    # Deactivate wandb
    os.environ["WANDB_MODE"] = "disabled"
    wandb.init()
    training_phase = preset_config.all_phases[0]
    assert training_phase.end_lr is not None  # To make pyright happy.
    trained_encoder = coolchic_train(
        frame_encoder=frame_enc,
        frame=frame,
        frame_encoder_manager=frame_encoder_manager,
        start_lr=training_phase.lr,
        end_lr=training_phase.end_lr,
        cosine_scheduling_lr=training_phase.schedule_lr,
        max_iterations=training_phase.max_itr,
        frequency_validation=training_phase.freq_valid,
        patience=training_phase.patience,
        optimized_module=training_phase.optimized_module,
        quantizer_type=training_phase.quantizer_type,
        quantizer_noise_type=training_phase.quantizer_noise_type,
        softround_temperature=training_phase.softround_temperature,
        noise_parameter=training_phase.noise_parameter,
        val_logs=None,
    )

    # Eval trained encoder.
    trained_encoder = quantize_model(
        trained_encoder, frame=frame, frame_encoder_manager=frame_encoder_manager
    )
    logs = test(trained_encoder, frame, frame_encoder_manager)
    metrics = log_to_results(logs, seq_name=img_path.stem)
    # We only return the last validation log, which is the only real valid number.
    return [metrics]


def finetune_one_kodak(
    image: Path,
    preset_config: PresetConfig,
    hypernet: WholeNet,
    dec_cfg: DecoderConfig,
    lmbda: float,
    from_scratch: bool = False,
) -> list[SummaryEncodingMetrics]:
    if from_scratch:
        # No need to load hypernet.
        coolchic_encoder_parameter = CoolChicEncoderParameter(
            **get_coolchic_param_from_args(dec_cfg)
        )
        img, _ = read_png(str(image))
        coolchic_encoder_parameter.set_image_size((img.shape[-2], img.shape[-1]))
        cc_encoder = CoolChicEncoder(coolchic_encoder_parameter)
    else:
        # Get coolchic representation from hypernet
        img, _ = read_png(str(image))
        with torch.no_grad():
            cc_encoder = hypernet.image_to_coolchic(img, stop_grads=True)

    res_metrics = finetune_coolchic(
        img_path=image,
        preset_config=preset_config,
        cc_encoder=cc_encoder,
        lmbda=lmbda,
        dec_cfg=dec_cfg,
    )
    del cc_encoder  # Free memory.
    return res_metrics


def finetune_all(
    preset: PresetConfig,
    from_scratch: bool,
    weights_path: Path,
    config_path: Path,
    wholenet_cls: type[WholeNet],
    dataset: DATASET_NAME,
    n_iterations: list[int],
) -> pd.DataFrame:
    # Load config and hypernet.
    cfg = load_config(config_path, HypernetRunConfig)
    assert isinstance(cfg.lmbda, float)  # To make pyright happy.
    if weights_path.stem == "__latest":
        weights_path = get_latest_checkpoint(weights_path.parent)
    hnet = load_hypernet(weights_path, cfg, wholenet_cls)
    hnet.eval()

    all_finetuned = []
    for image in (DATA_DIR / dataset).glob("*.png"):
        # Train until we have done the whole training phase,
        # we can only eval at the end of the whole phase.
        for n_iter in n_iterations:
            preset.all_phases[0].max_itr = n_iter
            img_name = image.stem
            print(f"Finetuning {img_name}")
            finetuned = finetune_one_kodak(
                image,
                preset,
                hypernet=hnet,
                dec_cfg=cfg.hypernet_cfg.dec_cfg,
                lmbda=cfg.lmbda,
                from_scratch=from_scratch,
            )
            all_finetuned.append(pd.DataFrame([log.model_dump() for log in finetuned]))
            torch.cuda.empty_cache()  # Free memory after each image.
            gc.collect()  # Collect garbage to free memory.
    return pd.concat(all_finetuned)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Finetune a hypernet's output, compare it with training from scratch."
    )
    # Comma-separated list of weight paths.
    parser.add_argument(
        "--weight_path", type=Path, required=True, help="Path to the hypernet weights."
    )
    parser.add_argument(
        "--wholenet_cls",
        type=str,
        default="CoolchicWholeNet",
        help="Class name of the WholeNet to use. "
        "Can be 'CoolchicWholeNet', 'DeltaWholeNet', or 'NOWholeNet'.",
    )
    parser.add_argument(
        "--config", type=Path, required=True, help="Path to the hypernet config."
    )
    parser.add_argument(
        "--from_scratch",
        action="store_true",
        help="If set, will train from scratch instead of finetuning.",
    )
    parser.add_argument(
        "--dataset",
        type=str,
        choices=["kodak", "clic20-pro-valid"],
        required=True,
        help="Dataset to use for finetuning. " "Can be 'kodak' or 'clic20-pro-valid'.",
    )
    args = parser.parse_args()

    # Configuring how training happens.
    training_phase = TrainerPhase(
        lr=5e-3,
        end_lr=1e-5,
        schedule_lr=True,
        max_itr=-1,
        freq_valid=100,
        patience=100,
        optimized_module=["all"],
        quantizer_type="softround",
        quantizer_noise_type="gaussian",
        softround_temperature=(0.3, 0.1),
        noise_parameter=(0.25, 0.1),
        quantize_model=True,
    )
    training_preset = PresetConfig(
        preset_name="", warmup=Warmup(), all_phases=[training_phase]
    )

    # Checking that the wholenet class is correct and assigning it.
    def get_wholenet_class(wholenet_cls: str) -> type[WholeNet]:
        match wholenet_cls:
            case "CoolchicWholeNet":
                return CoolchicWholeNet
            case "DeltaWholeNet":
                return DeltaWholeNet
            case "NOWholeNet":
                return NOWholeNet
            case _:
                raise ValueError(f"Invalid WholeNet class. Got {wholenet_cls}.")

    wholenet_cls = get_wholenet_class(args.wholenet_cls)

    finetuned = finetune_all(
        training_preset,
        weights_path=args.weight_path,
        config_path=args.config,
        from_scratch=args.from_scratch,
        wholenet_cls=wholenet_cls,
        dataset=args.dataset,
        n_iterations=[
            100,
            200,
            400,
            600,
            800,
            1000,
            1500,
            2000,
            2500,
            3000,
        ],  # Different iterations to test.
    )
    finetuned["anchor"] = (
        f"{args.wholenet_cls}-finetuning"
        if not args.from_scratch
        else "coolchic-training"
    )

    all_results = finetuned

    save_dir = RESULTS_DIR / "finetuning"
    save_dir = save_dir / args.dataset / args.weight_path.parent.parent.stem
    if args.from_scratch:
        save_dir = save_dir.parent / "from_scratch"
    save_dir.mkdir(parents=True, exist_ok=True)
    save_name = f"finetuning_{args.weight_path.parent.stem}.csv"
    save_path = save_dir / save_name
    all_results.to_csv(save_path, index=False)

    # only plot if not on server.
    if get_best_device() == "cpu":
        all_results = pd.read_csv(save_path)
        crossing_its: dict[
            str, dict[str, list[dict[Literal["hn", "scratch"], int]]]
        ] = {
            "jpeg": defaultdict(list),
            "hm": defaultdict(list),
            "hypernet": defaultdict(list),
        }
        for image in (DATA_DIR / args.dataset).glob("*.png"):
            # Skip images if they are not in the results.
            if image.stem not in all_results["seq_name"].values:
                continue
            plot_hypernet_rd(image.stem, all_results, args.dataset)
            for anchor_name in crossing_its:
                crossing_its[anchor_name][image.stem].append(
                    {
                        "hn": find_crossing_it(
                            image.stem,
                            all_results,
                            "nocc-finetuning",
                            anchor_name=anchor_name,
                            dataset=args.dataset,
                        ),
                        "scratch": find_crossing_it(
                            image.stem,
                            all_results,
                            "coolchic-training",
                            anchor_name=anchor_name,
                            dataset=args.dataset,
                        ),
                    }
                )

        for anchor_name, crossings_per_img in crossing_its.items():
            print(f"Crossing iterations for {anchor_name}")
            for seq_name, crossings in crossings_per_img.items():
                for cross in crossings:
                    print(
                        f"{seq_name:<40}, crossing iterations: "
                        f"hnet-finetuning: {cross['hn']*training_phase.freq_valid}, "
                        f"coolchic-training: {cross['scratch']*training_phase.freq_valid}"
                    )
        plt.show()
