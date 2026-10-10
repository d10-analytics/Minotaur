"""Direct behavioral coverage for the graph-model breadth-first traversal."""

from __future__ import annotations

from minotaur.graph_model.slicing import bfs


def test_seeds_only_at_depth_zero() -> None:
    adjacency = {"a": {"b"}}
    assert bfs(frozenset({"a"}), adjacency, 0) == {"a": 0}


def test_neighbors_reached_at_depth_one() -> None:
    adjacency = {"a": {"b"}, "b": {"c"}}
    assert bfs(frozenset({"a"}), adjacency, 1) == {"a": 0, "b": 1}


def test_unlimited_depth_traverses_a_cycle_once() -> None:
    adjacency = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
    assert bfs(frozenset({"a"}), adjacency, None) == {"a": 0, "b": 1, "c": 2}


def test_diamond_records_shortest_depth() -> None:
    adjacency = {"a": {"b", "c"}, "b": {"d"}, "c": {"d"}}
    assert bfs(frozenset({"a"}), adjacency, None) == {"a": 0, "b": 1, "c": 1, "d": 2}


def test_several_seeds_start_at_depth_zero() -> None:
    adjacency = {"a": {"b"}, "d": {"e"}}
    assert bfs(frozenset({"a", "d"}), adjacency, None) == {"a": 0, "d": 0, "b": 1, "e": 1}


def test_self_loop_stays_at_depth_zero() -> None:
    adjacency = {"a": {"a"}}
    assert bfs(frozenset({"a"}), adjacency, None) == {"a": 0}


def test_seed_missing_from_adjacency_is_the_only_node() -> None:
    adjacency = {"a": {"b"}}
    assert bfs(frozenset({"z"}), adjacency, None) == {"z": 0}
