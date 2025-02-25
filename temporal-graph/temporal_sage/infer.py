from asyncio.log import logger
import os
import numpy as np
import pandas as pd
import time
import torch
# from tqdm import tqdm, trange
import easydict
from sklearn.metrics import f1_score, roc_auc_score, accuracy_score, average_precision_score

from hdfs.client import Client
import json
import urllib.parse as urlparse
import pickle as pkl
import argparse
from collections import defaultdict

from batch_model import BatchModel
from util import set_logger, timestamp_transform
from build_data import get_data


def gen_org_co(args, df_pred, nid2oid, mode='format'):
    mode = "{}_data".format(mode)
    org_map = pd.read_csv("{}/{}/{}.orgmap".format(args.root_dir, mode, args.dataset))
    oid2oname = org_map.set_index('Index').to_dict()['_ID']

    df = df_pred[df_pred.label == 1]
    df['from_oid'] = df['from_nid'].map(nid2oid)
    df['to_oid'] = df['to_nid'].map(nid2oid)
    df['from_oname'] = df['from_oid'].map(oid2oname)
    df['to_oname'] = df['to_oid'].map(oid2oname)

    dic = {}
    for year in range(2000, 2022):
        dic[year] = defaultdict(int)

    for i in range(len(df)):
        src = df['from_oname'].iloc[i]
        tgt = df['to_oname'].iloc[i]
        year = int(df['timespan'].iloc[i])
        if src != tgt and src != 'NOT FOUND' and tgt != 'NOT FOUND':
            dic[year][(src, tgt)] += 1
    
    out = {}
    for year, dd in dic.items():
        out[year] = [[key[0], key[1], val] for key, val in dd.items()]
    return out


def gen_metrics(args, y_label, y_pred, y_prob, y_ts):
    rst = {}
    for year in range(args.timespan_start, args.timespan_end):
        idx = np.where(y_ts==year)[0]
        acc = accuracy_score(y_label[idx], y_pred[idx])
        ap = average_precision_score(y_label[idx], y_prob[idx])
        auc = roc_auc_score(y_label[idx], y_prob[idx])
        f1 = f1_score(y_label[idx], y_pred[idx])
        rst[year] = {'ACC': acc, 'F1': f1, 'AP': ap, 'AUC': auc}
    return rst


def infer_model(args, model, test_loader, features, nid2oid):
    model.eval()
    y_probs, y_labels = [], []
    from_nodes, to_nodes, y_timespan = [], [], []
    test_start = time.time()
    with torch.no_grad():
        # for step, (input_nodes, pos_graph, neg_graph, history_blocks) in enumerate(tqdm(test_loader, desc='infer')):
        batch_start = time.time()
        for step, (input_nodes, pos_graph, neg_graph, history_blocks) in enumerate(test_loader):
            history_inputs = [nfeat[nodes].to(args.device) for nfeat, nodes in zip(features, input_nodes)]
            # batch_inputs = nfeats[input_nodes].to(device)
            pos_graph = pos_graph.to(args.device)
            neg_graph = neg_graph.to(args.device)
            # blocks = [block.int().to(device) for block in blocks]
            history_blocks = [[block.int().to(args.device) for block in blocks] for blocks in history_blocks]

            pos_score, neg_score = model(history_blocks, history_inputs, pos_graph, neg_graph)
            y_probs.append(pos_score.detach().cpu().numpy())
            y_labels.append(np.ones_like(y_probs[-1]))
            y_probs.append(neg_score.detach().cpu().numpy())
            y_labels.append(np.zeros_like(y_probs[-1]))

            nids, cur_ts =  pos_graph.ndata['_ID'], pos_graph.edata['ts']
            from_node, to_node = pos_graph.edges()
            from_nodes.append(nids[from_node].detach().cpu().numpy())
            to_nodes.append(nids[to_node].detach().cpu().numpy())
            y_timespan.append(cur_ts.detach().cpu().numpy())

            from_node, to_node = neg_graph.edges()
            from_nodes.append(nids[from_node].detach().cpu().numpy())
            to_nodes.append(nids[to_node].detach().cpu().numpy())

            neg_num = len(from_node) // len(cur_ts)
            cur_ts = cur_ts.repeat_interleave(neg_num)
            y_timespan.append(cur_ts.detach().cpu().numpy())

            batch_time = time.time() - batch_start
            batch_start = time.time()

            if args.dgl_sampler:
                sampler_str = 'Using Dgl Neighbor Sampler.'
            else:
                sampler = test_loader.sampler
                start_time = np.min(sampler.resp_start_times)
                end_time = np.max(sampler.resp_end_times)
                query_counts = np.sum(sampler.resp_query_counts)
                node_counts = np.sum(sampler.resp_node_counts)
                sampler.clear_resp_metrics()
                resp_metric_str = f'{start_time},{end_time},{query_counts},{node_counts}\n'
                header = 'start_time,end_time,request_count,node_count\n'
                if args.rst_client.status(args.profile_path, strict=False) is None:
                    args.rst_client.write(args.profile_path, data=header, encoding='utf-8')
                args.rst_client.write(args.profile_path, data=resp_metric_str, encoding='utf-8', append=True)

                sampler_str = ' Sampler service costs total time {} milliseconds with {} queries.'.format(end_time - start_time, node_counts)
            # sampler_str = resp_metric_str
            batch_str = '\r Current batch: {}/{} costs {:.2f} seconds.'.format(str(step).zfill(4), len(test_loader), batch_time)
            print(batch_str + sampler_str, end='')

    y_prob = np.hstack([y.squeeze(1) for y in y_probs])
    y_pred = y_prob > 0.5
    y_label = np.hstack([y.squeeze(1) for y in y_labels])

    acc = accuracy_score(y_label, y_pred)
    ap = average_precision_score(y_label, y_prob)
    auc = roc_auc_score(y_label, y_prob)
    f1 = f1_score(y_label, y_pred)

    test_time = time.time() - test_start
    print('\nTest costs {:.2f} seconds.'.format(test_time))
    logger.info('Test ACC: %.4f, F1: %.4f, AP: %.4f, AUC: %.4f', acc, f1, ap, auc)
    df = pd.DataFrame({'ACC': [acc], 'F1': [f1], 'AP': [ap] ,'AUC': [auc]})
    with args.rst_client.write(args.rst_path, encoding='utf-8', overwrite=True) as writer:
        df.to_csv(writer, index=False)

    from_nid = np.hstack([y for y in from_nodes])
    to_nid = np.hstack([y for y in to_nodes])
    y_ts = np.hstack([y for y in y_timespan])
    df = pd.DataFrame({'from_nid': from_nid, 'to_nid': to_nid, 'label': y_label ,'prob': y_prob, 'timestamp': y_ts})
    with args.rst_client.write(args.pred_path, encoding='utf-8', overwrite=True) as writer:
        df.to_csv(writer, index=False)

    # visualization
    if args.dataset == 'DBLPV13':
        vis_rst = gen_metrics(args, y_label, y_pred, y_prob, y_ts)
        with args.rst_client.write(args.vis_rst_path, encoding='utf-8', overwrite=True) as writer:
            json.dump(vis_rst, writer)
        
        df = pd.DataFrame({'from_nid': from_nid, 'to_nid': to_nid, 'label': y_label ,'prob': y_prob, 'timespan': y_ts})
        vis_preds = gen_org_co(args, df, nid2oid, mode='format')
        with args.rst_client.write(args.vis_pred_path, encoding='utf-8', overwrite=True) as writer:
            json.dump(vis_preds, writer)
        
    return args.outfile_path


def run(args):
    logger.info(f'Loading dataset {args.dataset} from {args.root_dir}')
    test_loader, features, n_features, num_nodes, num_edges, nid2oid = get_data(args, logger, mode='infer')

    model = BatchModel(n_features, args.n_hidden, args.embed_dim, args.n_layers).to(args.device)
    logger.info(f'Loading model from {args.model_path}')

    with args.model_client.read(args.model_path) as reader:
        model_dict = pkl.load(reader)
    model.load_state_dict(model_dict)
    
    logger.info('Begin infering with %d nodes, %d edges.', num_nodes, num_edges)
    infer_model(args, model, test_loader, features, nid2oid)


def config2args(config, args):
    args.dataset = config['spaceId']
    timespan_start, timespan_end = timestamp_transform(config, args, logger)    
    args.timespan_start = timespan_start
    args.timespan_end = timespan_end
    logger.warning('%s training with time from %.0f to %.0f.', args.dataset, args.timespan_start, args.timespan_end)
    # args.dataset = 'DBLPV13'
    # args.timespan_start = 2001
    # args.timespan_end = 2020

    args.outfile_path = config['outFilePath']
    args.model_path = config['modelPath']
    args.feature_names = config['featureNames']
    txt = config['flinkFeatureNames']
    args.named_feats = 'all' # [ord(s.lower())-ord('a') for s in txt if ord('A') <= ord(s) <=ord('z')] if txt!='all' else 'all'
    args.dgl_sampler = config['dgl_sampler']
    args.old_sampler = config['old_sampler']
    # args.root_dir = config['dataPath']
    
    args.timespan_end += 1 # [] -> [), range left close right close -> left close right open
    args.num_ts += 1 # Changing left close right open to left close right close by adding 1 to args.timespan_end and args.num_ts 
    return args


def infer(config):
    args = easydict.EasyDict({
        'dataset': 'ia_contact', 
        'root_dir': './', 
        'prefix': 'TemporalSAGE', 
        'epochs': 50, 
        'bs': 1024, 
        'num_ts': 19,
        'n_hidden': 100, 
        'embed_dim': 100, 
        'n_layers': 2, 
        'gpu': 0, 
        'lr': 1e-2, 
        'temporal_feat': False, 
        'named_feats': 'all', 
        'timespan_start': -np.inf, 
        'timespan_end': np.inf, 
        'cpp_file': './wart-servers/examples/sampler.wasm',
    })

    logger = set_logger()
    args.device = torch.device(f'cuda:{args.gpu}') if torch.cuda.is_available() else torch.device('cpu')
    args = config2args(config, args)
    logger.info(args)

    # PARAM_STR = f'{args.epochs}-{args.bs}-{args.num_ts}-{args.n_hidden}'
    # PARAM_STR += f'-{args.embed_dim}-{args.n_layers}-{args.lr}'
    # PARAM_STR += f'-{args.temporal_feat}-{args.named_feats}'
    # SAVE_PATH = f'{args.prefix}-{PARAM_STR}-{args.timespan_start}-{args.timespan_end}-{args.dataset}'
    # SAVE_PATH = f'{args.dataset}'
    
    client_path, file_path = split_url(args.outfile_path)
    args.rst_client = Client(client_path)
    args.pred_path = os.path.join(file_path, 'prediction.csv')
    args.rst_path = os.path.join(file_path, 'infer_metrics.csv')
    args.profile_path = os.path.join(file_path, 'profile.csv')
    args.vis_rst_path = os.path.join(file_path, 'vis_metrics.json')
    args.vis_pred_path = os.path.join(file_path, 'vis_prediction.json')

    client_path, args.model_path = split_url(args.model_path)
    args.model_client = Client(client_path)

    run(args)
    return args.outfile_path


def split_url(url):
    uparse = urlparse.urlparse(url)
    client_path = "http://" + uparse.netloc
    file_path = uparse.path
    return client_path, file_path


def get_config(url):
    client_path, config_path = split_url(url)

    client = Client(client_path)
    lines = []
    with client.read(config_path) as reader:
        for line in reader:
            lines.append(line)
    lines_utf8 = [line.decode() for line in lines]
    lines_replace = [line.replace('\xa0', '') for line in lines_utf8]
    config = json.loads(''.join(lines_replace))
    return config


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", type=str, default='http://192.168.1.13:9009/dev/conf/infer.json')
    parser.add_argument("--dgl_sampler", "-s", action='store_true')
    parser.add_argument('--old_sampler', action='store_true')
    args = parser.parse_args()

    config = get_config(args.config)
    print(config)
    # config = {
    #     "taskId": "585838793082061314TSN",
    #     "spaceId": "DBLPV13",
    #     # "outFilePath": "./saved_models/",
    #     "outFilePath": "http://192.168.1.13:9009/dev/pytorch/infer-93B9AE168267/",
    #     # "modelPath": "./saved_models/dblp-coauthors_2epochs.pth",
    #     "modelPath": "http://192.168.1.13:9009/dev/pytorch/train-4FB003D56AAC/train-4FB003D56AAC.pth",
    #     "featureNames":"属性A,属性B,属性C",
    #     "flinkFeatureNames":"属性A,属性D,属性E",
    #     "startTime": "2001",
    #     "endTime": "2003",
    #     "trainTarget": 1,
    #     "dataPath": "./",
    #     "otherParam": "",
    #     "labelName": "1",
    #     "idIndex": "1"
    # }
    config['dgl_sampler'] = args.dgl_sampler
    config['old_sampler'] = args.old_sampler

    outfile_path = infer(config)
    print('outfile_path: ', outfile_path)