import os
import sys

# Add RCRank to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
rcrank_path = os.path.join(current_dir, "RCRank")
if rcrank_path not in sys.path:
    sys.path.append(rcrank_path)

import json
import logging

import pandas as pd
import torch
import torch.nn as nn

logger = logging.getLogger(__name__)
from model.modules.FuseModel.Attention import MultiHeadedAttention
from model.modules.FuseModel.CrossTransformer import CrossTransformer
from model.modules.QueryFormer.utils import Encoding

# Local imports from RCRank
from model.modules.rcrank_model import GateComDiffPretrainModel
from model.modules.TSModel.ts_model import CustomConvAutoencoder
from RCRank.utils.config import Args, ArgsPara
from RCRank.utils.plan_encoding import PlanEncoder
from transformers import BertModel, BertTokenizer


def resolve_bert_path(current_dir: str) -> str:
    """Resolve local BERT path or fallback to HuggingFace model id."""
    env_path = os.getenv("BERT_MODEL_PATH")
    if env_path and os.path.isdir(env_path):
        return env_path
    local_path = os.path.join(current_dir, "RCRank", "bert-base-uncased")
    if os.path.isdir(local_path):
        return local_path
    return "bert-base-uncased"


class RCRankPredictor:
    def __init__(self, config):
        self.device = config.get("device", "cpu")
        self.num_classes = config.get("num_classes", 4)
        self.model_path = config.get("model_path")
        train_data_path = config.get("train_data_path")

        # Get the absolute path to bert-base-uncased
        current_dir = os.path.dirname(os.path.abspath(__file__))
        bert_path = resolve_bert_path(current_dir)

        self.tokenizer = BertTokenizer.from_pretrained(bert_path)
        self.opt_threshold = config.get("opt_threshold", 0.5)
        self.pred_type = "multilabel"

        self.plan_args = Args()
        self.plan_args.device = self.device
        self.para_args = ArgsPara()

        self._compute_statistics(train_data_path)

        sql_model = BertModel.from_pretrained(bert_path)
        time_model = CustomConvAutoencoder()

        # initialize CrossTransformer and MultiHeadedAttention
        fuse_num_layers = 3
        fuse_head_size = 4
        emb_dim = 32
        fuse_ffn_dim = 128
        dropout = 0.1
        use_metrics = True
        use_log = True

        multihead_attn_modules_cross_attn = nn.ModuleList(
            [
                MultiHeadedAttention(
                    fuse_head_size,
                    emb_dim,
                    dropout=dropout,
                    use_metrics=use_metrics,
                    use_log=use_log,
                )
                for _ in range(fuse_num_layers)
            ]
        )
        fuse_model = CrossTransformer(
            num_layers=fuse_num_layers,
            d_model=emb_dim,
            heads=fuse_head_size,
            d_ff=fuse_ffn_dim,
            dropout=dropout,
            attn_modules=multihead_attn_modules_cross_attn,
        )

        r_attn_model = nn.ModuleList(
            [
                MultiHeadedAttention(
                    fuse_head_size,
                    emb_dim,
                    dropout=dropout,
                    use_metrics=use_metrics,
                    use_log=use_log,
                )
                for _ in range(fuse_num_layers)
            ]
        )
        rootcause_cross_model = CrossTransformer(
            num_layers=fuse_num_layers,
            d_model=emb_dim,
            heads=fuse_head_size,
            d_ff=fuse_ffn_dim,
            dropout=dropout,
            attn_modules=r_attn_model,
        )

        # initialize main model
        # Create model on CPU first to avoid meta tensor issues
        self.model = GateComDiffPretrainModel(
            t_input_dim=9,
            l_input_dim=13,
            l_hidden_dim=64,
            t_hidden_him=64,
            emb_dim=32,
            device="cpu",  # Create on CPU first
            plan_args=self.plan_args,
            sql_model=sql_model,
            cross_model=fuse_model,
            time_model=time_model,
            rootcause_cross_model=rootcause_cross_model,
        )

        # load pre-trained model first, then move to device
        # This avoids the "Cannot copy out of meta tensor" error
        print(f"Loading model from {self.model_path}")
        try:
            # Load state dict to CPU first
            state_dict = torch.load(self.model_path, map_location='cpu')
            self.model.load_state_dict(state_dict, strict=False)
            # Then move model to target device
            if self.device != "cpu":
                self.model = self.model.to(self.device)
        except Exception as e:
            # If loading fails, try the original method
            logger.warning(f"Failed to load model with strict=False, trying original method: {e}")
            try:
                # Try moving to device first, then loading
                if self.device != "cpu":
                    self.model = self.model.to(self.device)
                self.model.load_state_dict(torch.load(self.model_path, map_location=self.device), strict=False)
            except Exception as e2:
                logger.error(f"Failed to load model: {e2}")
                raise e2
        
        self.model.eval()

    def _compute_statistics(self, train_data_path):
        print(f"Computing statistics from training data: {train_data_path}")
        df = pd.read_csv(train_data_path)

        # delete error rows
        df = df[df["error"].isna()].copy()

        df["internal_metrics"] = df["internal_metrics"].apply(json.loads)
        df["external_metrics"] = df["external_metrics"].apply(json.loads)
        logs = []
        timeseries = []
        for _, row in df.iterrows():
            logs.append(torch.tensor(row["internal_metrics"]))
            timeseries.append(torch.tensor(row["external_metrics"]))

        logs = torch.stack(logs, dim=0)
        self.logs_train_mean = logs.mean(dim=0)
        self.logs_train_std = logs.std(dim=0)

        timeseries = torch.stack(timeseries, dim=0)
        self.timeseries_train_mean = timeseries.mean(dim=[0, 2])
        self.timeseries_train_std = timeseries.std(dim=[0, 2])
        print("Statistics computation completed")

    def predict(self, query, plan, timeseries, log):
        self.model.eval()
        
        with torch.no_grad():
            sql = self.tokenizer(
                query,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=512,
            ).to(self.device)

            plan = {
                "x": plan["x"].to(self.device).to(torch.float32),
                "attn_bias": plan["attn_bias"].to(self.device),
                "rel_pos": plan["rel_pos"].to(self.device),
                "heights": plan["heights"].to(self.device),
            }

            timeseries = torch.tensor(timeseries)
            if len(timeseries.shape) == 2:
                timeseries = timeseries.unsqueeze(0)  # add batch dimension
            # standardize
            timeseries = (timeseries - self.timeseries_train_mean.unsqueeze(1)) / (self.timeseries_train_std.unsqueeze(1) + 1e-6)
            timeseries = timeseries.to(self.device)

            log = torch.tensor(log)
            if len(log.shape) == 1:
                log = log.unsqueeze(0)  # add batch dimension
            # standardize
            log = (log - self.logs_train_mean) / (self.logs_train_std + 1e-6)
            log = log.to(self.device)

            pred_label_opt, _ = self.model(sql, plan, timeseries, log)
            pred_label = torch.where(pred_label_opt > self.opt_threshold, 1, 0)

            return pred_label_opt, pred_label


def load_sample_data(data_path):
    print("Loading sample data from", data_path)
    df = pd.read_csv(data_path)

    df = df[df["error"].isna()].copy()

    df["internal_metrics"] = df["internal_metrics"].apply(json.loads)
    df["external_metrics"] = df["external_metrics"].apply(json.loads)

    encoding = Encoding(None, {"NA": 0})

    pe = PlanEncoder(df=df, encoding=encoding)
    df = pe.df

    row = df.iloc[0]

    sample = {
        "query": row["query"],
        "plan": row["json_plan_tensor"],
        "log": row["internal_metrics"],
        "timeseries": row["external_metrics"],
        "multilabel": torch.tensor(eval(row["multilabel"])),
        "duration": row["duration"],
    }

    return sample


def online_predict(data_path, opt_threshold=0.5, num_classes=4):
    predictor = RCRankPredictor(
        model_path="model_res/GateComDiffPretrainModel slow_sql_data_gen eta0.07/best_model.pt",
        train_data_path=data_path,
        device="cpu",
        opt_threshold=opt_threshold,
        num_classes=num_classes,
    )

    sample = load_sample_data(data_path)

    pred_label, pred_label_binary = predictor.predict(
        query=sample["query"],
        plan=sample["plan"],
        timeseries=sample["timeseries"],
        log=sample["log"],
    )

    print("\nResults:")
    print("Input Query:", sample["query"])
    print("\nPredicted labels:", pred_label.cpu().numpy())
    print("Binary Predicted labels:", pred_label_binary.cpu().numpy())
    print("\nTrue labels:", sample["multilabel"].numpy())


if __name__ == "__main__":
    data_path = "./slow_sql_data_gen.csv"
    online_predict(data_path)
