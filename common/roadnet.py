"""Road graph helpers on the SUMO net (sumolib)."""
from __future__ import annotations

from functools import lru_cache

import sumolib

from common.settings import config, path


@lru_cache
def load_net() -> sumolib.net.Net:
    return sumolib.net.readNet(str(path(config()["sim"]["net_file"])), withInternal=False)


def upstream_distances(net: sumolib.net.Net, edge_id: str, pos: float, radius: float) -> dict[str, float]:
    """Map edge -> distance from the start of that edge to the hazard point at (edge_id, pos).

    A vehicle at lane position p on edge e is (result[e] - p) metres upstream of the hazard.
    Walks incoming edges (breadth-first) until the radius is covered.
    """
    out = {edge_id: pos}
    if not net.hasEdge(edge_id):
        return out
    frontier = [(net.getEdge(edge_id), pos)]
    while frontier:
        e, d = frontier.pop()
        for inc in e.getIncoming():
            if inc.getID() in out or inc.isSpecial():
                continue
            total = d + inc.getLength()
            out[inc.getID()] = total
            if total < radius:
                frontier.append((inc, total))
    return out
