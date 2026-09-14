import json
import sys
from collections import deque

sys.path.append(".")

import ast

import numpy as np
import torch

from ..model.modules.QueryFormer.utils import *


def node2feature(node, encoding, hist_file, table_sample):
    # type, join, filter123, mask123
    # 1, 1, 3x3 (9), 3
    # The pretrained RCRank input has room for 30 predicates per node.  Some
    # PostgreSQL plans (notably wide TPC-DS filters) contain more; retain the
    # first 30 instead of constructing a negative-width padding array.
    max_filters = 30
    num_filter = min(len(node.filterDict["colId"]), max_filters)
    pad = np.zeros((2, max_filters - num_filter))
    filts = np.array(list(node.filterDict.values()))[:, :max_filters]  # cols, ops, vals
    ## 3x3 -> 9, get back with reshape 3,3
    filts = np.concatenate((filts, pad), axis=1).flatten()
    mask = np.zeros(30)
    mask[:num_filter] = 1
    type_join = np.array([node.typeId, node.join])

    # table, bitmap, 1 + 1000 bits
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

    # np.concatenate((type_join, filts, table, sample, global_plan_cost))
    # type_join, filts, mask, table, sample, global_plan_cost, local_plan_cost
    # 2          60      30     1     1000        4                  4
    # (1,1,40,30,1001, 4)
    # return np.concatenate((type_join, filts, mask, table, sample, global_plan_cost, local_plan_cost), dtype=np.float64)
    return np.concatenate(
        (type_join, filts, mask, table, sample, global_plan_cost), dtype=np.float64
    )
    # typeId, joinId, filtersId, filtersMask, table_sample, cost
    # 1          1      40            30         1001         4


def f(s):
    if "    " in s and "\n" in s:
        return s.split("    ")[1].split("\n")[0]
    else:
        return s


class PlanEncoder:
    """
    sample: [feature, label]
    """

    def __init__(
        self, df, train=True, encoding=None, tokenizer=None, train_dataset=None
    ):
        super().__init__()
        self.encoding = encoding
        self.treeNodes = []

        # PostgreSQL's EXPLAIN (FORMAT JSON) normally returns
        # ``[{"Plan": ...}]``.  The original DREAM data loader expected an
        # older double-wrapped form and indexed ``[0][0]`` unconditionally.
        # Normalize both forms here, and also accept an already decoded value.
        df = df.copy()
        df.loc[:, "plan_json"] = df["plan_json"].apply(
            lambda value: json.dumps(value) if not isinstance(value, str) else value
        )

        df.loc[:, "json_plan_tensor"] = np.nan

        df = df[df["plan_json"].str.count("'Plans'") < 500]

        tmp = []
        # for i in tqdm(range(df.shape[0])):
        for i in range(df.shape[0]):
            # print("plan", df["plan_json"].iloc[i])
            # node = json.loads(df["plan_json"].iloc[i])['Plan']
            s = df["plan_json"].iloc[i]
            payload = s
            for _ in range(4):
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except (TypeError, json.JSONDecodeError):
                        payload = ast.literal_eval(f(payload))
                if isinstance(payload, list) and len(payload) == 1:
                    payload = payload[0]
                    continue
                break

            if not isinstance(payload, dict) or "Plan" not in payload:
                raise ValueError(f"Unsupported PostgreSQL plan JSON shape: {type(payload).__name__}")
            node = payload["Plan"]
            a = self.js_node2dict(i, node)
            # print(a)
            # df["json_plan_tensor"].iloc[i] = a
            tmp.append(a)

            # print("node", df["json_plan_tensor"].iloc[i])
            # print("node shape", df["json_plan_tensor"].iloc[i]["x"].shape, i)
        df["json_plan_tensor"] = tmp

        df = df[df["json_plan_tensor"] != np.nan]
        self.df = df
        # print(len(df))

    def js_node2dict(self, idx, node):
        treeNode = self.traversePlan(node, idx, self.encoding)
        _dict = self.node2dict(treeNode)
        collated_dict = self.pre_collate(_dict)

        self.treeNodes.clear()
        del self.treeNodes[:]

        return collated_dict

    ## pre-process first half of old collator
    def pre_collate(self, the_dict, max_node=500, rel_pos_max=20):

        x = pad_2d_unsqueeze(the_dict["features"], max_node)
        N = len(the_dict["features"])
        attn_bias = torch.zeros([N + 1, N + 1], dtype=torch.float)

        edge_index = the_dict["adjacency_list"].t()
        if len(edge_index) == 0:
            shortest_path_result = np.array([[0]])
            path = np.array([[0]])
            adj = torch.tensor([[0]]).bool()
        else:
            adj = torch.zeros([N, N], dtype=torch.bool)
            adj[edge_index[0, :], edge_index[1, :]] = True

            shortest_path_result = floyd_warshall_rewrite(adj.numpy())

        rel_pos = torch.from_numpy((shortest_path_result)).long()

        attn_bias[1:, 1:][rel_pos >= rel_pos_max] = float("-inf")

        attn_bias = pad_attn_bias_unsqueeze(attn_bias, max_node + 1)
        rel_pos = pad_rel_pos_unsqueeze(rel_pos, max_node)

        heights = pad_1d_unsqueeze(the_dict["heights"], max_node)

        if x is None:
            1 == 1
        return {"x": x, "attn_bias": attn_bias, "rel_pos": rel_pos, "heights": heights}

    def node2dict(self, treeNode):

        adj_list, num_child, features = self.topo_sort(treeNode)
        heights = self.calculate_height(adj_list, len(features))

        return {
            # 'features' : torch.FloatTensor(features),
            "features": torch.tensor(np.array(features), dtype=torch.float64),
            "heights": torch.LongTensor(heights),
            "adjacency_list": torch.LongTensor(np.array(adj_list)),
        }

    def topo_sort(self, root_node):
        #        nodes = []
        adj_list = []  # from parent to children
        num_child = []
        features = []

        toVisit = deque()
        toVisit.append((0, root_node))
        next_id = 1
        while toVisit:
            idx, node = toVisit.popleft()
            #            nodes.append(node)
            features.append(node.feature)
            num_child.append(len(node.children))
            for child in node.children:
                toVisit.append((next_id, child))
                adj_list.append((idx, next_id))
                next_id += 1

        return adj_list, num_child, features

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

    def calculate_height(self, adj_list, tree_size):
        if tree_size == 1:
            return np.array([0])

        adj_list = np.array(adj_list)
        node_ids = np.arange(tree_size, dtype=int)
        node_order = np.zeros(tree_size, dtype=int)
        uneval_nodes = np.ones(tree_size, dtype=bool)

        parent_nodes = adj_list[:, 0]
        child_nodes = adj_list[:, 1]

        n = 0
        while uneval_nodes.any():
            uneval_mask = uneval_nodes[child_nodes]
            unready_parents = parent_nodes[uneval_mask]

            node2eval = uneval_nodes & ~np.isin(node_ids, unready_parents)
            node_order[node2eval] = n
            uneval_nodes[node2eval] = False
            n += 1
        return node_order


def norm_cost():
    df = pd.read_pickle("data/test.pickle")
    plan_tensor = df["json_plan_tensor"]

    global_sum = None
    for i in range(df.shape[0]):
        if global_sum is None:
            global_sum = torch.log(
                df["json_plan_tensor"].iloc[i]["x"][:, :, -8:-4] + 1e-6
            )
        else:
            global_sum = torch.cat(
                [
                    global_sum,
                    torch.log(df["json_plan_tensor"].iloc[i]["x"][:, :, -8:-4] + 1e-6),
                ],
                dim=0,
            )

    for i in range(df.shape[0]):
        df["json_plan_tensor"].iloc[i]["x"][:, :, -8:-4] = torch.log(
            df["json_plan_tensor"].iloc[i]["x"][:, :, -8:-4] + 1e-6
        ) - torch.mean(global_sum, dim=[0, 1]) / (
            torch.std(global_sum, dim=[0, 1]) + 1e-9
        )
        df["json_plan_tensor"].iloc[i]["x"] = df["json_plan_tensor"].iloc[i]["x"][
            :, :, :-4
        ]
    # print(plan_tensor.iloc[0]["x"][:, :, -4:])
    # print(plan_tensor.iloc[0]["x"].shape)
    # print(global_sum.shape)
    df.to_pickle("data/test.pickle")


if __name__ == "__main__":
    df = pd.read_csv("data/temp_pretrain_data/query_ex_plan_json_plan1.csv")

    encoding = Encoding(None, {"NA": 0})
    encoder = PlanEncoder(df, encoding=encoding)
