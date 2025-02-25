import pandas as pd
import numpy as np
import torch
import dgl
from tqdm import trange
import pickle as pkl
from dgl.dataloading import negative_sampler
from batch_loader import TemporalEdgeDataLoader
from batch_loader2 import TemporalEdgeDataLoader2
import os

from sampler import MyMultiLayerSampler, NeublaMultiLayerSampler


def _load_data(dataset="ia-contact", mode="format_data", root_dir="./"):
    edges = pd.read_csv("{}/{}/{}.edges".format(root_dir, mode, dataset))
    nodes = pd.read_csv("{}/{}/{}.nodes".format(root_dir, mode, dataset))
    return edges, nodes
    

def load_data(dataset="ia-contact", mode="format", root_dir="./"):
    """We split dataset into two files: dataset.edges, and dataset.nodes.

    """
    # Load edges and nodes dataframes from the following directories.
    # Return: a tuple of (edges, nodes) of required datasets.
    # format_data/train_data/valid_data/test_data
    # label_train_data/label_valid_data/label_test_data
    mode = "{}_data".format(mode)
    return _load_data(dataset=dataset, mode=mode, root_dir=root_dir)


def covert_to_dgl(args, edges, nodes):
    assert(max(edges['from_node_id'].max(), edges['to_node_id'].max()) == nodes['node_id'].max())
    assert(min(edges['from_node_id'].min(), edges['to_node_id'].min()) == nodes['node_id'].min())
    assert(np.all(np.array(nodes['id_map'].tolist()) == np.arange(len(nodes))))

    min_ts, max_ts = edges['timestamp'].min(), edges['timestamp'].max() + 1 # left close right open
    if not (min_ts <= args.timespan_start < max_ts and min_ts < args.timespan_end <= max_ts):
        raise ValueError(f'startTime/endTime must be within the range [{min_ts}, {max_ts})')

    node2nid = nodes.set_index('node_id').to_dict()['id_map']
    edges['src_nid'] = edges['from_node_id'].map(node2nid)
    edges['dst_nid'] = edges['to_node_id'].map(node2nid)

    graph = dgl.graph((torch.tensor(edges['src_nid']), torch.tensor(edges['dst_nid'])))
    graph.edata['ts']  = torch.tensor(edges['timestamp'])
    graph.ndata['feat'] = torch.from_numpy(np.eye(len(nodes))).to(torch.float) # one_hot
    return graph


def split_graph(args, logger, graph, num_ts, mode):
    ts_start, ts_end = args.timespan_start, args.timespan_end
    if mode == 'infer':
        ts_start -= np.ceil((ts_end - ts_start) / num_ts)
        num_ts += 1
        logger.info(f'Inference needs one more history graph, so timespan:{args.timespan_start}->{ts_start}, num_ts:{num_ts-1}->{num_ts}')

    timespan = (ts_end - ts_start) / num_ts
    if timespan < 1.0:
        timespan = 1.0
        num_ts = int((ts_end - ts_start) / timespan)
        logger.warning(f'The number of snapshots is too large, and has been reduced to {num_ts}.')
    else:
        timespan = np.ceil(timespan)
    logger.info(f'Split graph into {num_ts} snapshots within the range [{ts_start}, {ts_end}), timespan is {timespan}.')
    graphs, time_range = [], []

    if args.temporal_feat:
        spath = os.path.join(args.root_dir, 'format_data/dblp-coauthors.nfeat.pkl')
        logger.info(f'Loading temporal node feature from {spath}')
        node_feats = pkl.load(open(spath, 'rb'))
        
    for i in trange(num_ts):
        ts_low = ts_start + i*timespan
        ts_high = ts_low + timespan

        eids = graph.filter_edges(lambda x: (x.data['ts']>= ts_low) & (x.data['ts'] < ts_high))
        ts_graph = graph.edge_subgraph(eids, preserve_nodes=True)

        if args.temporal_feat:
            ts_graph.ndata['feat'] = node_feats[int(ts_low)]

        # old_feat = ts_graph.ndata['feat']
        # if 'all' not in args.named_feats:
        #     feat_select = []
            
        #     for dim in args.named_feats:
        #         try:
        #             dim = int(dim)
        #         except:
        #             raise ValueError(f'--named_feats must be list(int), but {dim} is not a integer')
        #         feat_select.append(old_feat[:, dim:dim+1])
        #     ts_graph.ndata['feat'] = torch.hstack(feat_select)

        graphs.append(ts_graph)
        time_range.append((ts_low, ts_high))

    # new_feat = graphs[0].ndata['feat']
    # logger.info(f'Select these dims: {args.named_feats} and change node feats from {old_feat.shape} to {new_feat.shape}')
    return graphs, time_range


def get_data(args, logger, mode):
    edges, nodes = load_data(dataset=args.dataset, mode="format", root_dir=args.root_dir)
    graph = covert_to_dgl(args, edges, nodes)
    logger.info('Graph %s.', str(graph))
    coauthors, time_range = split_graph(args, logger, graph, num_ts=args.num_ts, mode=mode)

    # node_features = coauthors[0].ndata['feat']
    node_features = graph.ndata['feat']
    n_features = node_features.shape[1]
    
    # features = [g.ndata['feat'] for g in coauthors]
    features = [graph.ndata['feat']] * len(coauthors)
    num_nodes = coauthors[0].number_of_nodes()
    num_edges = sum([g.number_of_edges() for g in coauthors])
    # num_edges = graph.number_of_edges()

    if args.dgl_sampler:
        if dgl.__version__ > '0.8.0':
            from dgl.dataloading import NeighborSampler
            # sampler = NeighborSampler([15, 10])
            sampler = NeighborSampler([15])
        else:
            from dgl.dataloading.neighbor import MultiLayerNeighborSampler
            # sampler = MultiLayerNeighborSampler([15, 10])
            sampler = MultiLayerNeighborSampler([15])
    elif args.old_sampler:
        # sampler = MyMultiLayerSampler([15, 10], num_nodes=num_nodes, cpp_file = args.cpp_file, graph_name=args.dataset)
        sampler = MyMultiLayerSampler([15], num_nodes=num_nodes, cpp_file = args.cpp_file, graph_name=args.dataset)
    else:
        sampler = NeublaMultiLayerSampler([15], num_nodes, graph_name=args.dataset)
        # sampler = NeublaMultiLayerSampler([15, 10], num_nodes, graph_name=args.dataset)

    neg_sampler = negative_sampler.Uniform(5)
    data_range = list(range(1, int(len(coauthors))))
    
    # from dgl.dataloading.neighbor import MultiLayerNeighborSampler
    # sampler = NeublaMultiLayerSampler([15], num_nodes, graph_name=args.dataset)
    # dsampler = MultiLayerNeighborSampler([15])
    # samplers = (sampler, dsampler)
    # data_loader = TemporalEdgeDataLoader2(args.dgl_sampler, coauthors, data_range, time_range, 
    #     samplers, negative_sampler=neg_sampler, batch_size=args.bs, shuffle=False,
    #     drop_last=False, num_workers=0)
    
    data_loader = TemporalEdgeDataLoader(args.dgl_sampler, coauthors, data_range, time_range, 
        sampler, negative_sampler=neg_sampler, batch_size=args.bs, shuffle=False,
        drop_last=False, num_workers=0)

    if args.dataset == 'DBLPV13':
        nid2oid = nodes.set_index('id_map').to_dict()['org']
    else:
        nid2oid = {}
    return data_loader, features, n_features, num_nodes, num_edges, nid2oid