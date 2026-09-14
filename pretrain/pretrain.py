import json
import math
import os
import sys
import argparse

import torch.optim as optim

# Add RCRank root to Python path
current_dir = os.path.dirname(os.path.abspath(__file__))
rcrank_root = os.path.dirname(current_dir)
if rcrank_root not in sys.path:
    sys.path.append(rcrank_root)
rcrank_parent = os.path.dirname(rcrank_root)
if rcrank_parent not in sys.path:
    sys.path.append(rcrank_parent)

import random

import torch
import torch.nn as nn
import tqdm
from model.modules.LogModel.log_model import LogModel
from model.modules.QueryFormer.QueryFormer import QueryFormer
from model.modules.QueryFormer.utils import *
from RCRank.utils.plan_encoding import PlanEncoder
from torch.utils.data import DataLoader, Dataset
from transformers import BertModel, BertTokenizer


class Encoding:
    def __init__(
        self, column_min_max_vals, col2idx, op2idx={">": 0, "=": 1, "<": 2, "NA": 3}
    ):
        # self.column_min_max_vals = column_min_max_vals
        self.col2idx = col2idx
        self.op2idx = op2idx

        idx2col = {}
        for k, v in col2idx.items():
            idx2col[v] = k
        self.idx2col = idx2col
        self.idx2op = {0: ">", 1: "=", 2: "<", 3: "NA"}

        self.type2idx = {}
        self.idx2type = {}
        self.join2idx = {}
        self.idx2join = {}

        self.table2idx = {"NA": 0}
        self.idx2table = {0: "NA"}

    def normalize_val(self, column, val, log=False):
        mini, maxi = self.column_min_max_vals[column]

        val_norm = 0.0
        if maxi > mini:
            val_norm = (val - mini) / (maxi - mini)
        return val_norm

    def encode_filters(self, filters=[], alias=None):
        ## filters: list of dict

        #        print(filt, alias)
        if len(filters) == 0:
            return {"colId": [self.col2idx["NA"]], "opId": [self.op2idx["NA"]]}  # ,
            #    'val': [0.0]}
        # res = {'colId':[],'opId': [],'val': []}
        res = {"colId": [], "opId": []}
        for filt in filters:
            filt = "".join(c for c in filt if c not in "()")
            # fs = filt.split(' AND ')
            fs = re.split(" AND | OR ", filt)
            for f in fs:
                #           print(filters)
                op = None
                for k, v in self.idx2op.items():
                    if v in f:
                        op = v
                if op is None:
                    op = "NA"
                # col, op, num = f.split(' ')[0], f.split(' ')[1], ' '.join(f.split(' ')[2:])
                col = f.split(" ")[0]
                if alias is None:
                    column = col
                else:
                    column = alias + "." + col
                #            print(f)
                if column not in self.col2idx:
                    self.col2idx[column] = len(self.col2idx)
                    self.idx2col[self.col2idx[column]] = column

                res["colId"].append(self.col2idx[column])
                res["opId"].append(self.op2idx[op])
                # res['val'].append(self.normalize_val(column, float(num)))
                # res['val'].append(num)
        return res

    def encode_join(self, join):
        if join not in self.join2idx:
            self.join2idx[join] = len(self.join2idx)
            self.idx2join[self.join2idx[join]] = join
        return self.join2idx[join]

    def encode_table(self, table):
        if table not in self.table2idx:
            self.table2idx[table] = len(self.table2idx)
            self.idx2table[self.table2idx[table]] = table
        return self.table2idx[table]

    def encode_type(self, nodeType):
        if nodeType not in self.type2idx:
            self.type2idx[nodeType] = len(self.type2idx)
            self.idx2type[self.type2idx[nodeType]] = nodeType
        return self.type2idx[nodeType]


def node2feature(node, encoding, hist_file, table_sample):
    num_filter = len(node.filterDict["colId"])
    pad = np.zeros((2, 30 - num_filter))
    filts = np.array(list(node.filterDict.values()))  # cols, ops, vals
    ## 3x3 -> 9, get back with reshape 3,3
    filts = np.concatenate((filts, pad), axis=1).flatten()
    mask = np.zeros(30)
    mask[:num_filter] = 1
    type_join = np.array([node.typeId, node.join])

    table = np.array([node.table_id])
    sample = np.zeros(1000)

    global_plan_cost = np.array(
        [
            float(node.start_up_cost),
            float(node.total_cost),
            float(node.plan_rows),
            float(node.plan_width),
        ]
    )
    local_plan_cost = np.array(
        [
            float(node.start_up_cost),
            float(node.total_cost),
            float(node.plan_rows),
            float(node.plan_width),
        ]
    )

    return np.concatenate(
        (type_join, filts, mask, table, sample, global_plan_cost), dtype=np.float64
    )


def get_table_mask_feature(plan, encoding):
    plan = json.loads(plan)
    nodeType = plan["Node Type"]
    typeId = encoding.encode_type(nodeType)
    card = None  # plan['Actual Rows']
    filters, alias = formatFilter(plan)
    join = formatJoin(plan)
    joinId = encoding.encode_join(join)
    filters_encoded = encoding.encode_filters(filters, alias)
    root = TreeNode(
        nodeType,
        typeId,
        filters,
        card,
        joinId,
        join,
        filters_encoded,
        plan["Startup Cost"],
        plan["Total Cost"],
        plan["Plan Rows"],
        plan["Plan Width"],
    )
    root.table = plan["Relation Name"]
    root.table_id = encoding.encode_table(plan["Relation Name"])
    return node2feature(root, encoding, None, None)


class Predict(nn.Module):
    def __init__(self, input_dim, model_dim, num_heads, ff_dim, dropout=0.1):
        super(Predict, self).__init__()
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=model_dim,
            nhead=num_heads,
            dim_feedforward=ff_dim,
            dropout=dropout,
            batch_first=True,
        )
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=6)
        self.input_fc = nn.Linear(input_dim, 1100, bias=False)
        self.input_fc1 = nn.Linear(1100, model_dim, bias=False)

    def forward(self, x):
        x = self.input_fc(x)
        x = self.input_fc1(x)
        x = self.transformer_encoder(x)
        return x


class Alignment(nn.Module):
    def __init__(self, device, n_class=0):
        super(Alignment, self).__init__()
        self.flatten = nn.Flatten()
        self.plan_model = QueryFormer(pred_hid=32, input_size=1097)
        # 使用相对路径，基于当前模块所在目录
        bert_path = os.path.join(rcrank_root, "bert-base-uncased")
        self.sql_model = BertModel.from_pretrained(bert_path)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=768, nhead=8, dim_feedforward=2048, dropout=0.1, batch_first=True
        )
        self.transformer_encoder = nn.TransformerEncoder(encoder_layer, num_layers=1)
        # 使用相对路径，基于当前模块所在目录
        self.tokenizer = BertTokenizer.from_pretrained(bert_path)
        self.log_model = LogModel(input_dim=13, hidden_dim=64, output_dim=32)

        self.concat_dim_mask_plan = 3 * 768
        self.predict_mask_plan = Predict(
            input_dim=self.concat_dim_mask_plan,
            model_dim=1024,
            num_heads=8,
            ff_dim=2048,
        )
        self.Linear_mask_plan = nn.Linear(1024, 287)
        self.Linear_plan = nn.Sequential(
            nn.Linear(16032, 1536), nn.ReLU(), nn.Linear(1536, 768)
        )

        self.concat_dim_mask_sql = 3 * 768
        self.predict_mask_sql = Predict(
            input_dim=self.concat_dim_mask_sql, model_dim=768, num_heads=8, ff_dim=2048
        )
        self.Linear_mask_sql = nn.Linear(768, 768)
        self.device = device

    def forward(self, plan, sql, log, dic, mod):
        plan = self.plan_model(plan)
        sql = self.tokenizer(
            sql, return_tensors="pt", padding=True, truncation=True, max_length=512
        ).to(self.device)
        sql = self.sql_model(**sql).last_hidden_state
        sql = self.transformer_encoder(sql).mean(dim=1)
        log = self.log_model(log)
        plan = self.flatten(plan)
        plan = self.Linear_plan(plan)
        concatenated_vector = torch.cat((plan, sql, dic), dim=1).unsqueeze(1)
        if mod == "mask_plan":
            transformer_output = self.predict_mask_plan(concatenated_vector)
            transformer_output = transformer_output[:, 0, :]
            predicted_vector = self.Linear_mask_plan(transformer_output)

        elif mod == "mask_sql":
            transformer_output = self.predict_mask_sql(concatenated_vector)
            transformer_output = transformer_output[:, 0, :]
            predicted_vector = self.Linear_mask_sql(transformer_output)
        return predicted_vector


class PretrainDataset(Dataset):
    def __init__(self, path, tokenizer, bert, device):

        print("processing data.....")
        df = pd.read_pickle(path)
        df = df[["query", "plan_json", "log_all"]]

        df_plan = pd.DataFrame(columns=["query", "plan_json", "log_all", "predict"])
        df_sql = pd.DataFrame(columns=["query", "plan_json", "log_all", "predict"])
        encoding = Encoding(None, {"NA": 0, "MASK": -1})

        self.treeNodes = []
        self.tables = []
        df = df[df["plan_json"].str.count("Plan") < 100]
        error = 0
        for i in tqdm.tqdm(range(len(df))):
            plan = df.iloc[i]["plan_json"]
            query = df.iloc[i]["query"]
            self.tables = []
            self.treeNodes = []
            self.traversePlan(json.loads(plan)["Plan"], 0, encoding)

            if len(self.tables) > 0:
                mask_id = math.floor(len(self.tables) * random.random())
                plan_mask = plan.replace(
                    f'"Relation Name": "{self.tables[mask_id]}"',
                    '"Relation Name": "[MASK]"',
                )
                node = None
                target_pos = plan.find(f'"Relation Name": "{self.tables[mask_id]}"')
                start_pos = plan.rfind("{", 0, target_pos)
                end_pos = plan.find('"}', target_pos) + 1
                plans_pos = plan.find(', "Plans"', target_pos)
                plans_pos = plan.find(', "Plans"', target_pos)

                a_pos = re.search(r": \d+\}", plan[target_pos:])
                if a_pos:
                    a_pos = a_pos.end() + target_pos - 1
                else:
                    a_pos = -1
                if plans_pos != -1 and end_pos > plans_pos:
                    end_pos = plans_pos
                if end_pos != -1 and a_pos != -1 and a_pos < end_pos:
                    end_pos = a_pos
                elif end_pos == -1 and a_pos != -1:
                    end_pos = a_pos
                mask_node = plan[start_pos:end_pos] + "}"

                query_mask = query.replace(self.tables[mask_id], "[MASK]")
                token = tokenizer(
                    self.tables[mask_id],
                    return_tensors="pt",
                    padding=True,
                    truncation=True,
                    max_length=512,
                ).to(device)
                table = bert(**token).pooler_output.detach().cpu().squeeze(0)
                df_sql.loc[len(df_sql)] = [
                    query_mask,
                    df.iloc[i]["plan_json"],
                    df.iloc[i]["log_all"],
                    table,
                ]
                try:
                    # torch.Tensor(get_table_mask_feature(mask_node,encoding))
                    df_plan.loc[len(df_plan)] = [
                        df.iloc[i]["query"],
                        plan_mask,
                        df.iloc[i]["log_all"],
                        torch.tensor(encoding.table2idx[self.tables[mask_id]]),
                    ]
                except Exception as e:
                    error += 1
                    print(f"{error} ==================>")

        self.mask_plan = PlanEncoder(df_plan, encoding=encoding).df
        self.mask_sql = PlanEncoder(df_sql, encoding=encoding).df
        self.encoding = encoding
        self.device = device

    def traversePlan(self, plan, idx, encoding):  # bfs accumulate plan

        nodeType = plan["Node Type"]
        typeId = encoding.encode_type(nodeType)
        card = None  # plan['Actual Rows']
        filters, alias = formatFilter(plan)
        join = formatJoin(plan)
        joinId = encoding.encode_join(join)
        filters_encoded = encoding.encode_filters(filters, alias)
        root = TreeNode(
            nodeType,
            typeId,
            filters,
            card,
            joinId,
            join,
            filters_encoded,
            plan["Startup Cost"],
            plan["Total Cost"],
            plan["Plan Rows"],
            plan["Plan Width"],
        )

        self.treeNodes.append(root)

        if "Relation Name" in plan:
            root.table = plan["Relation Name"]
            root.table_id = encoding.encode_table(plan["Relation Name"])
            self.tables.append(plan["Relation Name"])
        root.query_id = idx

        root.feature = node2feature(root, encoding, None, None)
        #    print(root)
        if "Plans" in plan:
            for subplan in plan["Plans"]:
                subplan["parent"] = plan
                node = self.traversePlan(subplan, idx, encoding)
                node.parent = root
                root.addChild(node)
        return root

    def __len__(self):
        return len(self.mask_plan)

    def __getitem__(self, idx):
        device = self.device
        line = self.mask_plan.iloc[idx]
        query = line["query"]
        x1 = line["json_plan_tensor"]["x"].squeeze(0)
        attn_bias = line["json_plan_tensor"]["attn_bias"].squeeze(0)
        rel_pos = line["json_plan_tensor"]["rel_pos"].squeeze(0)
        height = line["json_plan_tensor"]["heights"].squeeze(0)
        log_all = line["log_all"]
        predict = line["predict"]

        plan = {}
        plan["x"] = x1.to(torch.float32).to(device)
        plan["attn_bias"] = attn_bias.to(device)
        plan["rel_pos"] = rel_pos.to(device)
        plan["heights"] = height.to(device)

        mask_plan = {
            "query": query,
            "plan": plan,
            "log": torch.tensor(log_all).to(device),
            "predict": predict.to(device),
        }

        line_sql = self.mask_sql.iloc[idx]
        query_sql = line_sql["query"]
        x1_sql = line_sql["json_plan_tensor"]["x"].squeeze(0)
        attn_bias_sql = line_sql["json_plan_tensor"]["attn_bias"].squeeze(0)
        rel_pos_sql = line_sql["json_plan_tensor"]["rel_pos"].squeeze(0)
        height_sql = line_sql["json_plan_tensor"]["heights"].squeeze(0)
        log_all_sql = line_sql["log_all"]
        predict_sql = line_sql["predict"]

        plan = {}
        plan["x"] = x1_sql.to(torch.float32).to(device)
        plan["attn_bias"] = attn_bias_sql.to(device)
        plan["rel_pos"] = rel_pos_sql.to(device)
        plan["heights"] = height_sql.to(device)
        mask_sql = {
            "query": query_sql,
            "plan": plan,
            "log": torch.tensor(log_all_sql).to(device),
            "predict": predict_sql.to(device),
        }

        return mask_plan, mask_sql


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RCRank multimodal pre-training")
    parser.add_argument("--data_path", default=os.path.join(rcrank_root, "pretrain", "pretrain_data.pkl"))
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch_size", type=int, default=128)
    parser.add_argument("--output_dir", default=os.path.join(rcrank_root, "pretrain", "save"))
    args = parser.parse_args()

    device = args.device
    # 使用相对路径，基于当前模块所在目录
    bert_path = os.path.join(rcrank_root, "bert-base-uncased")
    tokenizer = BertTokenizer.from_pretrained(bert_path)
    bert = BertModel.from_pretrained(bert_path).to(device)
    dataset = PretrainDataset(args.data_path, tokenizer, bert, device)

    encoding = tokenizer(
        json.dumps(dataset.encoding.idx2table),
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=512,
    ).to(device)

    model = Alignment(device, n_class=len(dataset.encoding.idx2table.keys())).to(device)

    criterion = {"mask_sql": nn.MSELoss(), "mask_plan": nn.CrossEntropyLoss()}
    optimizer = optim.Adam(model.parameters(), lr=1e-5)
    epochs = args.epochs
    saved = 0
    best = 0.0
    ep = tqdm.tqdm(range(epochs))
    best_val_loss = float("inf")
    batch_size = args.batch_size
    batch_loss = {"mask_sql": 0.0, "mask_plan": 0.0}
    os.makedirs(args.output_dir, exist_ok=True)
    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=False)

    for epoch in ep:
        for i, batch in enumerate(dataloader):
            mask_plan, mask_sql = batch
            model.train()
            epoch_loss = 0.0
            data_batch = {"mask_plan": mask_plan, "mask_sql": mask_sql}
            task_list = ["mask_plan", "mask_sql"]
            for mod in task_list:
                batch_list = data_batch[mod]
                optimizer.zero_grad()
                sql, plan, log, predict = (
                    batch_list["query"],
                    batch_list["plan"],
                    batch_list["log"],
                    batch_list["predict"],
                )
                dic = bert(**encoding).pooler_output.repeat(len(sql), 1)
                output = model(plan, sql, log, dic, mod)
                loss = criterion[mod](output, predict)
                loss.backward()
                optimizer.step()
                batch_loss[mod] = loss.item()
                ep.set_postfix(
                    Batch_Loss_Mask_Plan=f"{batch_loss['mask_plan']/10:.4f}",
                    Batch_Loss_Mask_SQL=f"{batch_loss['mask_sql']:.4f}",
                    sample=f'{i*batch_size+len(batch_list["query"])}/{len(dataset)}',
                    refresh=True,
                )

            if (epoch + 1) % 10 == 0:
                torch.save(model.state_dict(), os.path.join(args.output_dir, f"model{(epoch+1)}.pth"))
