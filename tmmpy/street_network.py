import pandas as pd
import geopandas as gpd
import numpy as np
import networkx as nx

from shapely import geometry
from shapely.ops import nearest_points
from itertools import product
from itertools import chain

import matplotlib.pyplot as plt

from shapely.geometry import LineString
from shapely.geometry import Point

from networkx import connected_component_subgraphs, weakly_connected_component_subgraphs

import fiona


class StreetNetwork:
    """Class for representing various data derived from the data source.  
    Intended to support using the data to easily work with street networks as state spaces.
    """

    def __init__(self, data):
        assert data.nodes_df.crs == data.ways_df.crs
        self.nodes_df = data.nodes_df
        self.ways_df = data.ways_df
        self.edges_df = self.create_edges_df(
            self.ways_df, self.nodes_df, crs=data.nodes_df.crs
        )
        self.linestring_lookup = self.create_linestring_lookup()

        self.graph = self.create_graph()

        self.graph = self.trim_graph()
        self.trim_edges_df()
        self.trim_nodes_df()

    @staticmethod
    def create_edges_df(ways_df, nodes_df, crs):
        """Creates a dataframe consisting of the individual segments that make out the ways in the data source."""
        gdf_list = list()
        for _, row in ways_df.iterrows():
            osmid = row["osmid"]
            us = row["nodes"][:-1]
            vs = row["nodes"][1:]
            ls = row["linestring"]
            coords = list(ls.coords)
            lines = [LineString([i, j]) for i, j in zip(coords[:-1], coords[1:])]
            gdf = gpd.GeoDataFrame(
                {
                    "u": pd.Series(us, dtype=np.int64),
                    "v": pd.Series(vs, dtype=np.int64),
                    "osmid": osmid,
                    "line": lines,
                },
                geometry="line",
            )
            gdf["node_set"] = pd.Series(
                [tuple(sorted([u, v])) for u, v in zip(gdf.u, gdf.v)]
            )
            gdf_list.append(gdf)

        edges_df = gpd.GeoDataFrame(pd.concat(gdf_list), geometry="line", crs=crs)
        edges_df = edges_df.drop_duplicates("node_set")

        edges_df.drop("osmid", axis=1, inplace=True)
        edges_df = (
            edges_df.merge(
                nodes_df[["x", "y", "point", "osmid"]], left_on="u", right_on="osmid"
            )
            .rename(columns={"x": "ux", "y": "uy", "point": "u_point"})
            .drop("osmid", axis=1)
            .merge(
                nodes_df[["x", "y", "point", "osmid"]], left_on="v", right_on="osmid"
            )
            .drop("osmid", axis=1)
            .rename(columns={"x": "vx", "y": "vy", "point": "v_point"})
        )

        edges_df["length"] = edges_df.line.map(lambda x: x.length)

        return edges_df

    def trim_graph(self):
        return max(connected_component_subgraphs(self.graph), key=len)

    def create_linestring_lookup(self):
        return {
            tuple(sorted(edge)): line
            for edge, line in zip(self.edges_df.node_set, self.edges_df.line)
        }

    def create_graph(self):
        """Creates a graph, with each node being a node from the data source and each edge being an individual segment
        from the segment that makes out the ways."""
        graph = nx.Graph()
        graph.add_nodes_from(self.nodes_df.osmid)
        graph.add_edges_from(
            [
                (edge[0], edge[1], {"length": length, "line": line})
                for edge, length, line in zip(
                    self.edges_df.node_set, self.edges_df.length, self.edges_df.line
                )
            ]
        )
        return graph

    def trim_edges_df(self):
        """Remove rows in edges_df that are not part of the largest connected component."""
        edges_in_graph = set(map(lambda x: tuple(sorted(x)), self.graph.edges.keys()))
        self.edges_df = self.edges_df[self.edges_df.node_set.isin(edges_in_graph)]

    def trim_nodes_df(self):
        """Remove nodes in nodes_df that are not part of the largest connected component."""
        nodes_in_graph = set(self.graph.nodes.keys())
        self.nodes_df = self.nodes_df[self.nodes_df.osmid.isin(nodes_in_graph)]

    def shortest_path_length_between_nodes(self, l_node: int, r_node: int):
        """Find the length of the shortest path between two nodes."""
        return nx.shortest_path_length(
            self.graph, source=l_node, target=r_node, weight="length"
        )

    def shortest_path_length_between_edges(self, l_edge, r_edge):
        """Find the length of the shortest path connecting two edges."""
        return min(
            (
                self.shortest_path_length_between_nodes(nodes[0], nodes[1])
                for nodes in product(l_edge, r_edge)
            )
        )

    def distance_from_point_to_edge(self, point: Point, edge: tuple):
        """Find the distance from a point to the nearest point on the edge."""
        line = self.linestring_lookup[tuple(sorted(edge))]
        closest_points = nearest_points(point, line)
        ls = LineString([(p.x, p.y) for p in closest_points])
        return ls.length


class DirectedStreetNetwork(StreetNetwork):
    @staticmethod
    def create_edges_df(ways_df, nodes_df, crs):
        intermediate_dfs = []
        for _, row in ways_df.iterrows():
            point_coords = list(row["linestring"].coords)
            segment = [
                LineString([i, j]) for i, j in zip(point_coords[:-1], point_coords[1:])
            ]
            nodes = row["nodes"]
            if ("oneway", "yes") in row["tags"].items():
                edges = [
                    (from_node, to_node)
                    for from_node, to_node in zip(nodes[:-1], nodes[1:])
                ]
            else:
                edges = [
                    (from_node, to_node)
                    for from_node, to_node in zip(nodes[:-1], nodes[1:])
                ]
                edges += list(map(lambda x: tuple(reversed(x)), edges))
                segment += segment
            u = list(map(lambda x: x[0], edges))
            v = list(map(lambda x: x[1], edges))
            df = pd.DataFrame(
                {"u": u, "v": v, "node_set": edges, "linestring": segment}
            )
            intermediate_dfs.append(df)
        gdf = gpd.GeoDataFrame(
            pd.concat(intermediate_dfs), geometry="linestring", crs=crs
        )
        gdf["length"] = gdf.linestring.map(lambda x: x.length)
        return gdf

    def create_graph(self):
        graph = nx.DiGraph()
        graph.add_nodes_from(
            pd.Series(self.edges_df.u.tolist() + self.edges_df.v.tolist()).unique()
        )
        graph.add_edges_from(
            [
                (edge[0], edge[1], {"length": length})
                for edge, length in zip(self.edges_df.node_set, self.edges_df.length)
            ]
        )
        return graph

    def trim_graph(self):
        return max(weakly_connected_component_subgraphs(self.graph), key=len)

    def trim_edges_df(self):
        """Remove rows in edges_df that are not part of the largest connected component."""
        edges_in_graph = list(self.graph.edges.keys())
        self.edges_df = self.edges_df[self.edges_df.node_set.isin(edges_in_graph)]
