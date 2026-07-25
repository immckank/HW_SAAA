from __future__ import annotations

import sys
import unittest
from pathlib import Path

import torch
from torch_geometric.data import Data


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ActiveLearning"))

from train import _build_batches  # noqa: E402


def _graph(graph_id: str, n_nodes: int, n_edges: int) -> Data:
    return Data(
        x=torch.zeros((n_nodes, 3)),
        edge_index=torch.zeros((2, n_edges), dtype=torch.long),
        y=torch.tensor([0.0]),
        sample_weight=torch.tensor([1.0]),
        graph_id=graph_id,
    )


class TrainBatchingTest(unittest.TestCase):
    def test_respects_batch_size_when_no_size_cap(self) -> None:
        graphs = [_graph(f"g{i}", 10, 10) for i in range(5)]
        batches = _build_batches(
            graphs,
            batch_size=2,
            max_batch_nodes=0,
            max_batch_edges=0,
            shuffle=False,
        )
        self.assertEqual([[0, 1], [2, 3], [4]], [batch for batch in batches])

    def test_splits_when_node_budget_exceeded(self) -> None:
        graphs = [
            _graph("small-a", 100, 10),
            _graph("small-b", 100, 10),
            _graph("large", 60_000, 10),
        ]
        batches = _build_batches(
            graphs,
            batch_size=8,
            max_batch_nodes=50_000,
            max_batch_edges=0,
            shuffle=False,
        )
        self.assertEqual(2, len(batches[0]))
        self.assertEqual(["large"], [item.graph_id for item in batches[1]])

    def test_oversized_graph_trains_alone(self) -> None:
        graphs = [_graph("huge", 90_000, 120_000)]
        batches = _build_batches(
            graphs,
            batch_size=8,
            max_batch_nodes=50_000,
            max_batch_edges=80_000,
            shuffle=False,
        )
        self.assertEqual([["huge"]], [[item.graph_id for item in batch] for batch in batches])


if __name__ == "__main__":
    unittest.main()
