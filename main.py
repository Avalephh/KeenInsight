import argparse
import sys
from pathlib import Path

import torch
from model.train_test import train as train_opt_all

RCRANK_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = RCRANK_ROOT.parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dream.agent.plan.RCRank.utils.config import Args, ArgsPara, TrainConfig
from dream.agent.plan.RCRank.utils.load_data import load_dataset_valid as load_dataset_tensor_valid


def train(
    config,
    train_dataloader,
    test_dataloader,
    valid_dataloader,
    train_len,
    test_len,
    valid_len,
    train_dataset,
    plan_args,
    para_args,
    seed,
):
    train_opt_all(
        config.select_model,
        config.use_fuse_model,
        batch_size=config.batch_size,
        train_dataloader=train_dataloader,
        test_dataloader=test_dataloader,
        valid_dataloader=valid_dataloader,
        train_len=train_len,
        test_len=test_len,
        valid_len=valid_len,
        train_dataset=train_dataset,
        use_metrics=config.use_metrics,
        use_log=config.use_log,
        model_path_dir=config.model_path,
        model_name=config.model_name,
        lr=config.lr,
        epoch=config.epoch,
        plan_args=plan_args,
        para_args=para_args,
        config=config,
        seed=seed,
    )


parser = argparse.ArgumentParser()
parser.add_argument("--device", type=str, default="cuda:1")
parser.add_argument("--dataset", type=str, default="tpc_h")
parser.add_argument("--batch_size", type=int, default=8)
parser.add_argument("--data_path", type=str, default=None)
parser.add_argument("--epochs", type=int, default=50)
args = parser.parse_args()

if __name__ == "__main__":
    valid_dataloader = None
    valid_len = 0

    batch_size = args.batch_size
    device = args.device
    use_valid_dataset = True
    print(torch.cuda.is_available())

    rcrank_root = Path(__file__).resolve().parent
    data_path = args.data_path or str(rcrank_root / "data" / f"{args.dataset}.csv")

    (
        train_dataloader,
        test_dataloader,
        valid_dataloader,
        train_len,
        test_len,
        valid_len,
        train_dataset,
    ) = load_dataset_tensor_valid(data_path, batch_size=batch_size, device=device)

    config = TrainConfig()
    config.batch_size = batch_size
    config.lr = 1e-4
    config.epoch = args.epochs
    config.dataset = args.dataset

    plan_args = Args()
    plan_args.device = device
    para_args = ArgsPara()

    # 只使用多标签分类，不使用回归
    config.use_margin_loss = False
    config.use_threshold_loss = False
    config.use_weight_loss = False
    config.use_label_loss = True  # 只使用BCE损失

    # 设置多标签损失权重
    para_args.mul_label_weight = 1.0
    para_args.pred_type = "multilabel"  # 设置为多标签预测模式

    model_name = "GateComDiffPretrainModel"
    config.model_name = model_name
    config.use_fuse_model = True
    config.use_metrics = True
    config.use_log = True

    eta = 0.07
    config.margin_loss_margin = eta
    seed = 0
    config.model_path = str(rcrank_root / "res" / f"{config.model_name} {args.dataset} eta{eta}")
    train(
        config,
        train_dataloader,
        test_dataloader,
        valid_dataloader,
        train_len,
        test_len,
        valid_len,
        train_dataset,
        plan_args,
        para_args,
        seed,
    )
