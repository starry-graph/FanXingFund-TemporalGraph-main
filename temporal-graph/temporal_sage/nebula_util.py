import torch
import query_graph
from concurrent.futures import ThreadPoolExecutor
from tqdm import tqdm

class LayerParam:
    def __init__(self, edge_type, **kwargs):
        self.edge_type = edge_type
        self.limit = kwargs.get("limit", -1)
        self.min_time = kwargs.get("min_time", -1)
        self.max_time = kwargs.get("max_time", -1)
        
        d = kwargs.get("direct", "src_to_dst")
        if d == "src_to_dst":
            self.direction = 0
        elif d == "dst_to_src":
            self.direction = 1
        elif d == "bidirect":
            self.direction = 2
        else:
            raise ValueError("invalid direction")
        
    def to_cpp(self):
        return query_graph.LayerParam(
            self.edge_type,
            self.limit,
            (self.min_time, self.max_time),
            self.direction,
        )

class QueryGraphChannel:
    def __init__(self, addresses, max_workers=25):
        self.channel = query_graph.QueryGraphChannel(addresses, max_workers)
        self.executor = ThreadPoolExecutor(max_workers)
    
    def close(self, wait = False):
        self.executor.shutdown(wait)
    
    def __enter__(self):
        return self
    
    def __exit__(self, type, value, tb):
        self.close()
    
    def _do_sample(self, space_name, start_nodes, attrs, params):
        start_nodes = start_nodes.contiguous()
        params = [p.to_cpp() if isinstance(p, LayerParam) else LayerParam(**p).to_cpp() for p in params]
        edge_index = self.channel.sample_subgraph(space_name, start_nodes, params)
        
        # print(edge_index.size())
        
        total_nodes = edge_index[:2].flatten().unique()
        node_index, node_attrs = self.channel.gather_attributes(space_name, total_nodes, attrs)
        assert total_nodes.size() == node_index.size()
        
        # print(node_index)
        # print(node_attrs)
        
        return {
            "node_index": node_index,
            "node_attrs": node_attrs,
            "edge_index": edge_index,
        }
    
    def sample_subgraph(self, space_name, start_nodes, attrs, params):
        return self.executor.submit(self._do_sample, space_name, start_nodes, attrs, params)

if __name__ == '__main__':
    with QueryGraphChannel(["192.168.1.11:9669"]) as channel:
        space_name = "DBLPV13"
        start_nodes = torch.arange(64, dtype=torch.long)
        params = [
            {
                "edge_type": "coauthor",
                # "limit": 20,
                "direct": "bidirect",
            }
        ]

        import time
        s = time.time()
        n = 1000
        futures = []
        for future in tqdm([channel.sample_subgraph(space_name, start_nodes, ["author_id", "label"], params) for _ in range(n)]):
            futures.append(future)
        for future in tqdm(futures):
            future.result()
        t = time.time()
        print((t - s) / n)
