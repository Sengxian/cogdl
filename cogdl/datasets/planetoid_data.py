import os.path as osp
import pickle as pkl
import sys

import numpy as np
import torch
from cogdl.data import Data, Dataset
from cogdl.utils import download_url, remove_self_loops

from . import register_dataset


def coalesce(row, col, value=None):
    num = col.shape[0] + 1
    print("Num edges:", num)
    idx = torch.full((num,), -1, dtype=torch.float)
    idx[1:] = row * num + col
    mask = idx[1:] > idx[:-1]

    if mask.all():
        return row, col.value
    row = row[mask]
    col = col[mask]
    if value is not None:
        pass
    return row, col, value


def parse_index_file(filename):
    index = []
    for line in open(filename):
        index.append(int(line.strip()))
    return index


def index_to_mask(index, size):
    mask = torch.full((size,), False, dtype=torch.bool)
    mask[index] = True
    return mask


def edge_index_from_dict(graph_dict, num_nodes=None):
    row, col = [], []
    for key, value in graph_dict.items():
        row.append(np.repeat(key, len(value)))
        col.append(value)
    _row = np.concatenate(np.array(row))
    _col = np.concatenate(np.array(col))
    edge_index = np.stack([_row, _col], axis=0)

    row_dom = edge_index[:, _row > _col]
    col_dom = edge_index[:, _col > _row][[1, 0]]
    edge_index = np.concatenate([row_dom, col_dom], axis=1)
    _row, _col = edge_index

    edge_index = np.stack([_row, _col], axis=0)

    order = np.lexsort((_col, _row))
    edge_index = edge_index[:, order]

    edge_index = torch.tensor(edge_index, dtype=torch.long)
    # There may be duplicated edges and self loops in the datasets.
    row, col, _ = coalesce(edge_index[0], edge_index[1])
    edge_index = torch.stack([row, col], dim=0)
    edge_index, _ = remove_self_loops(edge_index)
    row = torch.cat([edge_index[0], edge_index[1]])
    col = torch.cat([edge_index[1], edge_index[0]])
    edge_index = torch.stack([row, col])
    print(edge_index.shape)
    return edge_index


def read_planetoid_data(folder, prefix):
    prefix = prefix.lower()
    names = ["x", "tx", "allx", "y", "ty", "ally", "graph", "test.index"]
    objects = []
    for item in names[:-1]:
        with open(f"{folder}/ind.{prefix}.{item}", "rb") as f:
            if sys.version_info > (3, 0):
                objects.append(pkl.load(f, encoding="latin1"))
            else:
                objects.append(pkl.load(f))
    test_index = parse_index_file(f"{folder}/ind.{prefix}.{names[-1]}")
    test_index = torch.Tensor(test_index).long()
    test_index_reorder = test_index.sort()[0]

    x, tx, allx, y, ty, ally, graph = tuple(objects)
    x, tx, allx = tuple([torch.from_numpy(item.todense()).float() for item in [x, tx, allx]])
    y, ty, ally = tuple([torch.from_numpy(item).float() for item in [y, ty, ally]])

    train_index = torch.arange(y.size(0), dtype=torch.long)
    val_index = torch.arange(y.size(0), y.size(0) + 500, dtype=torch.long)

    if prefix.lower() == "citeseer":
        # There are some isolated nodes in the Citeseer graph, resulting in
        # none consecutive test indices. We need to identify them and add them
        # as zero vectors to `tx` and `ty`.
        len_test_indices = (test_index.max() - test_index.min()).item() + 1

        tx_ext = torch.zeros(len_test_indices, tx.size(1))
        tx_ext[test_index_reorder - test_index.min(), :] = tx
        ty_ext = torch.zeros(len_test_indices, ty.size(1))
        ty_ext[test_index_reorder - test_index.min(), :] = ty

        tx, ty = tx_ext, ty_ext

    x = torch.cat([allx, tx], dim=0).float()
    y = torch.cat([ally, ty], dim=0).max(dim=1)[1].long()

    x[test_index] = x[test_index_reorder]
    y[test_index] = y[test_index_reorder]

    train_mask = index_to_mask(train_index, size=y.size(0))
    val_mask = index_to_mask(val_index, size=y.size(0))
    test_mask = index_to_mask(test_index, size=y.size(0))

    edge_index = edge_index_from_dict(graph, num_nodes=y.size(0))

    data = Data(x=x, edge_index=edge_index, y=y)
    data.train_mask = train_mask
    data.val_mask = val_mask
    data.test_mask = test_mask

    return data


class Planetoid(Dataset):
    r"""The citation network datasets "Cora", "CiteSeer" and "PubMed" from the
    `"Revisiting Semi-Supervised Learning with Graph Embeddings"
    <https://arxiv.org/abs/1603.08861>`_ paper.
    """

    url = "https://github.com/kimiyoung/planetoid/raw/master/data"

    def __init__(self, root, name, split="public", num_train_per_class=20, num_val=500, num_test=1000):
        self.name = name

        super(Planetoid, self).__init__(root)
        self.data = torch.load(self.processed_paths[0])

        self.split = split
        assert self.split in ["public", "full"]

        self.raw_dir = osp.join(self.root, self.name, "raw")
        self.processed_dir = osp.join(self.root, self.name, "processed")

        if split == "full":
            data = self.get(0)
            data.train_mask.fill_(True)
            data.train_mask[data.val_mask | data.test_mask] = False
            self.data = data

    @property
    def raw_file_names(self):
        names = ["x", "tx", "allx", "y", "ty", "ally", "graph", "test.index"]
        return ["ind.{}.{}".format(self.name.lower(), name) for name in names]

    @property
    def processed_file_names(self):
        return "data.pt"

    @property
    def num_classes(self):
        assert hasattr(self.data, "y")
        return int(torch.max(self.data.y)) + 1

    def download(self):
        for name in self.raw_file_names:
            download_url("{}/{}".format(self.url, name), self.raw_dir)

    def process(self):
        data = read_planetoid_data(self.raw_dir, self.name)
        torch.save(data, self.processed_paths[0])

    def get(self, idx):
        return self.data

    def __repr__(self):
        return "{}()".format(self.name)


def normalize_feature(data):
    x_sum = torch.sum(data.x, dim=1)
    x_rev = x_sum.pow(-1).flatten()
    x_rev[torch.isnan(x_rev)] = 0.0
    x_rev[torch.isinf(x_rev)] = 0.0
    data.x = data.x * x_rev.unsqueeze(-1).expand_as(data.x)
    return data


@register_dataset("cora")
class CoraDataset(Planetoid):
    def __init__(self, args=None):
        dataset = "Cora"
        path = osp.join(osp.dirname(osp.realpath(__file__)), "../..", "data", dataset)
        if not osp.exists(path):
            Planetoid(path, dataset)
        super(CoraDataset, self).__init__(path, dataset)
        self.data = normalize_feature(self.data)


@register_dataset("citeseer")
class CiteSeerDataset(Planetoid):
    def __init__(self, args=None):
        dataset = "CiteSeer"
        path = osp.join(osp.dirname(osp.realpath(__file__)), "../..", "data", dataset)
        if not osp.exists(path):
            Planetoid(path, dataset)
        super(CiteSeerDataset, self).__init__(path, dataset)
        self.data = normalize_feature(self.data)


@register_dataset("pubmed")
class PubMedDataset(Planetoid):
    def __init__(self, args=None):
        dataset = "PubMed"
        path = osp.join(osp.dirname(osp.realpath(__file__)), "../..", "data", dataset)
        if not osp.exists(path):
            Planetoid(path, dataset)
        super(PubMedDataset, self).__init__(path, dataset)
        self.data = normalize_feature(self.data)
