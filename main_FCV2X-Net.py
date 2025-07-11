import argparse
import json
import os
import math
import pickle
import random
import networkx as nx
import sys
import time
from collections import defaultdict, deque
from copy import deepcopy
import signal
import setproctitle
from pyproj import Proj

import numpy as np
import torch
import torch.nn.functional as F
from moss import Engine, TlPolicy, Verbosity
from torch import nn, optim
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from dqn_utils.utils.models import R_Actor, VR_Actor, J_Actor, VJ_Actor, BGCN_Actor
from dqn_utils.utils.config import get_config
from moss.export import DBRecorder
from torch.optim.lr_scheduler import LambdaLR

torch.set_float32_matmul_precision('medium')

def decompose_action(x, sizes):
    out = []
    for i in sizes:
        x, r = divmod(x, i)
        out.append(r)
    return out

def _t2n(x):
    return x.detach().cpu().numpy()

class Env:
    def __init__(self, data_path, step_size, step_count, log_dir, reward, base_algo, tl_interval, yellow_time=0, save=False, record=0, args=None, reward_weight=1):
        self.log_dir = log_dir
        if not os.path.exists(self.log_dir+'details'):  
            os.makedirs(self.log_dir+'details', exist_ok=True)
        MAP_PATH = f'{data_path}/map.pb'
        TRIP_PATH = f'{data_path}/persons.pb'
        self.eng = Engine(
            name='Routing',
            map_file=MAP_PATH,
            person_file=TRIP_PATH,
            start_step=0,
            step_interval=1,
            seed=43,
            verbose_level=Verbosity.NO_OUTPUT,
            person_limit=-1,
            junction_yellow_time=1,
            phase_pressure_coeff=1.5,
            speed_stat_interval=0,
            out_xmin=-1e999,
            out_ymin=-1e999,
            out_xmax=1e999,
            out_ymax=1e999,
            device=0
        )
        self.alpha = args.alpha
        self.junction_reward = args.junction_reward
        self.record = record
        if record == 1:
            self.recorder = DBRecorder(
                self.eng,
                "postgres://sim:tnBB0Yf4tm2fIrUi1KB2LqqZTqyrztSa@pg-fib.db.fiblab.tech/simulation",
                "srt.map_grid_network1",  # map collection used for webui-backend
                "south_beijing",
            )  # used for PostgreSQL output (optional)
        
        print('Engines created!')
        M = self.eng.get_map(False)
        self.M = M
        self.proj=Proj(M.header.projection)
        vehs = self.eng.get_persons(False)

        self.veh_dest_lanes = [m.schedules[0].trips[0].end.lane_position.lane_id for m in vehs.persons]
        self.veh_dest_roads = np.array([m.schedules[0].trips[0].routes[0].driving.road_ids[-1] for m in vehs.persons])  
        max_veh_id = max([m.id for m in vehs.persons])
        self.veh_id2idxs = np.zeros(max_veh_id+1, dtype=int)

        self.tl_interval = tl_interval

        for i, m in enumerate(vehs.persons):
            self.veh_id2idxs[m.id] = i

        self.target_vehicle_id = args.target_vehicle_id
        self.target_vehicle_idx = self.veh_id2idxs[self.target_vehicle_id]

        self.lane_id2idxs = {}
        for i, lane in enumerate(M.lanes):
            self.lane_id2idxs[lane.id] = i
        self.lane_id2idxs = self.lane_id2idxs
        lane_start_xs = np.array([M.lanes[i].center_line.nodes[0].x for i in range(len(M.lanes))])
        lane_start_ys = np.array([M.lanes[i].center_line.nodes[0].y for i in range(len(M.lanes))])
        lane_end_xs = np.array([M.lanes[i].center_line.nodes[-1].x for i in range(len(M.lanes))])
        lane_end_ys = np.array([M.lanes[i].center_line.nodes[-1].y for i in range(len(M.lanes))])
        self.lane_start_xs, self.lane_start_ys = lane_start_xs, lane_start_ys
        self.road_start_xs = np.array([lane_start_xs[self.lane_id2idxs[road.lane_ids[0]]] for road in M.roads])
        self.road_start_ys = np.array([lane_start_ys[self.lane_id2idxs[road.lane_ids[0]]] for road in M.roads])
        self.road_end_xs = np.array([lane_end_xs[self.lane_id2idxs[road.lane_ids[-1]]] for road in M.roads])
        self.road_end_ys = np.array([lane_end_ys[self.lane_id2idxs[road.lane_ids[-1]]] for road in M.roads])
        self.veh2dest = {veh.id: veh.schedules[0].trips[0].routes[0].driving.road_ids[-1] for veh in vehs.persons}

        self.source_state_dim = 3
        self.neighbor_state_dim = 6
        self.edge_dim = 2

        if os.path.exists(f'{data_path}/selected_person_ids.json'):
            with open(f'{data_path}/selected_person_ids.json', 'r') as f:       ### 只对这些vehs做observe和routing
                self.selected_person_ids = json.load(f)
        else:
            self.selected_person_ids = list(self.veh2dest.keys())

        self.selected_person_masks = np.array([0]*len(self.veh2dest))
        for i, veh in enumerate(self.veh2dest):
            if veh in self.selected_person_ids:
                self.selected_person_masks[i] = 1

        self.vehicles = {}
        self._step = 0
        self.eng.set_tl_duration_batch(list(range(self.eng.junction_count)), tl_interval)
        self.eng.set_tl_policy_batch(list(range(self.eng.junction_count)), base_algo)

        self._cid = self.eng.make_checkpoint()
        self.step_size = step_size
        self.step_count = step_count
        self.reward = reward
        self.info = {
            'ATT': 1e999,
            'Throughput': 0,
            'rewards': 0,
            'distance_reward': 0,
            'time_reward': 0,
            'ATT_finished': 1e999,
            'VEH': 0
        }
        self.success_travel_time, self.success_travel = 0, 0
        self.total_travel_time, self.total_travel = 0, 0
        self.total_emissions = 0
        self.data_path = data_path
        
        ## 对所有的路学习相接
        laneid2features = {}
        for lane in M.lanes:
            laneid2features[lane.id] = lane

        road2adjroad = {}
        for lane in M.lanes:
            if str(lane.parent_id)[0] == '3':     #junction
                continue
            for lane_successor in lane.successors:
                if str(laneid2features[lane_successor.id].parent_id)[0] == '3':     #junction
                    tmp = laneid2features[laneid2features[lane_successor.id].successors[0].id].parent_id
                    if str(tmp)[0] == '3' or tmp == lane.parent_id:
                        continue
                    if lane.parent_id not in road2adjroad:
                        road2adjroad[lane.parent_id] = set([tmp])
                    else:
                        road2adjroad[lane.parent_id].add(tmp)
        for road in road2adjroad:
            road2adjroad[road] = list(road2adjroad[road])

        self.max_action_size = max([len(road2adjroad[road]) for road in road2adjroad])

        road2preroad = {}
        for road in road2adjroad:
            for adjroad in road2adjroad[road]:
                if adjroad not in road2preroad:
                    road2preroad[adjroad] = set([road])
                else:
                    road2preroad[adjroad].add(road)
        for road in road2preroad:
            road2preroad[road] = list(road2preroad[road])

        self.road2adjroad = road2adjroad
        available_actions = {}
        for road in road2adjroad:
            available_actions[road] = np.zeros(self.max_action_size, dtype=int)
            available_actions[road][:len(road2adjroad[road])] = 1
            available_actions[road] = available_actions[road].tolist()
        self.available_actions = available_actions

        ### 对road之间构图，需要同步更新所有road的状态。这样的话需要对road进行重编号

        lane_list = []
        road_length = []
        roadid2distance = {}
        self.road_id2idxs, self.road_idxs2id = {}, {}
        for idx, road in enumerate(M.roads):
            lane_list.append(road.lane_ids)
            road_length.append(M.lanes[self.lane_id2idxs[road.lane_ids[0]]].length)
            roadid2distance[road.id] = M.lanes[self.lane_id2idxs[road.lane_ids[0]]].length
            self.road_id2idxs[road.id] = idx
            self.road_idxs2id[idx] = road.id

        self.num_roads = len(M.roads)
        
        self.roadidx2adjroadidx = {}
        for road in road2adjroad:
            self.roadidx2adjroadidx[self.road_id2idxs[road]] = [self.road_id2idxs[adjroad] for adjroad in road2adjroad[road]]

        ### 获取每个road的lane list
        def lane_numpy(lanes_list):
            max_lane_num = max([len(i) for i in lanes_list])
            lanes_array = np.zeros((len(lanes_list), max_lane_num), dtype=int)
            lanes_zero = np.zeros((len(lanes_list), max_lane_num), dtype=int)
            for i, lanes in enumerate(lanes_list):
                lanes_array[i, :len(lanes)] = lanes
                lanes_zero[i, len(lanes):] = 1
            return lanes_array, lanes_zero
        
        self.lanes_list, self.lanes_zero = lane_numpy(lane_list) 

        ### 获取junction信息
        self.junction_id2idxs, self.junction_idxs2id = {}, {}
        if os.path.exists(f'{data_path}/selected_junction_ids.json'):
            jids = json.load(open(f'{data_path}/selected_junction_ids.json'))
        else:
            jids = [junction.id for junction in M.junctions]   
        
        all_jids = [junction.id for junction in M.junctions] 
        for id in jids:
            self.junction_id2idxs[id] = all_jids.index(id)
            self.junction_idxs2id[all_jids.index(id)] = id

        ### 获取每个相位的lane list
        def phase_lanes_numpy():
            in_lanes_list = []
            out_lanes_list = []
            phase_lanes_list = []

            if os.path.exists(f'{data_path}/selected_junction_ids.json'):
                jids = json.load(open(f'{data_path}/selected_junction_ids.json'))
            else:
                jids = [junction.id for junction in M.junctions]   

            for jid in jids:
                junction = M.junctions[self.junction_id2idxs[jid]]
                phase_lanes, in_lanes, out_lanes = [], [], []
                labels = []

                junction_lanes = junction.lane_ids
                junction_in_lanes = np.array([laneid2features[lane_id].predecessors[0].id for lane_id in junction_lanes])
                junction_out_lanes = np.array([laneid2features[lane_id].successors[0].id for lane_id in junction_lanes])

                for phase in junction.fixed_program.phases:
                    green_signal = np.array([1 if j == 2 and laneid2features[i].turn != 3 else 0 for i, j in zip(junction_lanes, phase.states)])
                    in_lane = junction_in_lanes[green_signal==1].tolist()
                    out_lane = junction_out_lanes[green_signal==1].tolist()
                    in_lanes += in_lane
                    out_lanes += out_lane
                    phase_lanes.append([list(set(in_lane)), list(set(out_lane))])

                in_lanes_list.append(list(set(in_lanes)))
                out_lanes_list.append(list(set(out_lanes)))
                phase_lanes_list.append(phase_lanes)
            
            return in_lanes_list, out_lanes_list, phase_lanes_list
    
        self.in_lanes, self.out_lanes, self.phase_lanes = phase_lanes_numpy()
        self.in_lane_array, self.zero_in_lane_array = lane_numpy(self.in_lanes)
        self.max_junction_action_size = max([len(i) for i in self.phase_lanes])
        self.one_hot_mapping_matrix = np.eye(self.max_junction_action_size)

        def junction_in_lane_numpy(in_lanes_list):
            max_lane_num = max([len(i) for i in in_lanes_list])
            in_lanes_array = np.zeros((len(in_lanes_list), max_lane_num), dtype=int)
            in_lanes_zero = np.zeros((len(in_lanes_list), max_lane_num), dtype=int)
            for i, lanes in enumerate(in_lanes_list):
                in_lanes_array[i, :len(lanes)] = lanes
                in_lanes_zero[i, len(lanes):] = 1
            return in_lanes_array, in_lanes_zero
        
        def junction_phase_lane_numpy(phase_lanes_list):
            max_inflow_lane_num = max_outflow_lane_num = max(max([max([len(j[1]) for j in i]) for i in phase_lanes_list]),  max([max([len(j[0]) for j in i]) for i in phase_lanes_list]))
            
            phase_lanes_inflow = np.zeros((len(phase_lanes_list), self.max_junction_action_size, max_inflow_lane_num), dtype=int)
            phase_lanes_outflow = np.zeros((len(phase_lanes_list), self.max_junction_action_size, max_outflow_lane_num), dtype=int)
            zero_lanes_inflow = np.zeros((len(phase_lanes_list), self.max_junction_action_size, max_inflow_lane_num), dtype=int)
            zero_lanes_outflow = np.zeros((len(phase_lanes_list), self.max_junction_action_size, max_outflow_lane_num), dtype=int)
            non_zeros_inflow = np.zeros((len(phase_lanes_list), self.max_junction_action_size, max_inflow_lane_num), dtype=int)
            non_zeros_outflow = np.zeros((len(phase_lanes_list), self.max_junction_action_size, max_outflow_lane_num), dtype=int)
            missing_lanes_inflow = np.zeros((len(phase_lanes_list), self.max_junction_action_size, max_inflow_lane_num), dtype=int)
            missing_lanes_outflow = np.zeros((len(phase_lanes_list), self.max_junction_action_size, max_outflow_lane_num), dtype=int)
            missing_phase = np.zeros((len(phase_lanes_list), self.max_junction_action_size), dtype=int)

            for i, phase_lane in enumerate(phase_lanes_list):
                for j, lanes in enumerate(phase_lane):
                    phase_lanes_inflow[i, j, :len(lanes[0])] = lanes[0]
                    phase_lanes_outflow[i, j, :len(lanes[1])] = lanes[1]
                    zero_lanes_inflow[i, j, len(lanes[0]):] = 1
                    zero_lanes_outflow[i, j, len(lanes[1]):] = 1
                    non_zeros_inflow[i, j, :len(lanes[0])] = 1
                    non_zeros_outflow[i, j, :len(lanes[1])] = 1
                if len(phase_lane) < self.max_junction_action_size:
                    for j in range(len(phase_lane), self.max_junction_action_size):
                        missing_lanes_inflow[i, j, :] = 1
                        missing_lanes_outflow[i, j, :] = 1
                        non_zeros_inflow[i, j, :] = 1
                        non_zeros_outflow[i, j, :] = 1
                        missing_phase[i, j] = 1
            return phase_lanes_inflow, phase_lanes_outflow, zero_lanes_inflow, zero_lanes_outflow, missing_lanes_inflow, missing_lanes_outflow, non_zeros_inflow, non_zeros_outflow, missing_phase

        ### 获取路口转向可走的相位选择
        def phase_road_encoder():
            road2phase = {}
            road2junction = {}

            if os.path.exists(f'{data_path}/selected_junction_ids.json'):
                valid_jids = json.load(open(f'{data_path}/selected_junction_ids.json'))
            else:
                valid_jids = [junction.id for junction in M.junctions]   

            for jid in valid_jids:
                junction = M.junctions[self.junction_id2idxs[jid]]

                for phase_idx, phase in enumerate(junction.fixed_program.phases):
                    ### 右转选择
                    right_green_signal = np.array([1 if j == 2 and laneid2features[i].turn == 3 else 0 for i, j in zip(junction.lane_ids, phase.states)])
                    right_in_lane = np.array([laneid2features[lane_id].predecessors[0].id for lane_id in junction.lane_ids])[right_green_signal==1].tolist()
                    right_out_lane = np.array([laneid2features[lane_id].successors[0].id for lane_id in junction.lane_ids])[right_green_signal==1].tolist()

                    right_in_lane_parents = [laneid2features[lane_id].parent_id for lane_id in right_in_lane]
                    right_out_lane_parents = [laneid2features[lane_id].parent_id for lane_id in right_out_lane]

                    for (in_lane_parent, out_lane_parent) in zip(right_in_lane_parents, right_out_lane_parents):
                        if (in_lane_parent, out_lane_parent) not in road2phase:
                            road2phase[(in_lane_parent, out_lane_parent)] = [0, 0, 0, 0]   ### 不构成相位观察里的内容
                            road2junction[(in_lane_parent, out_lane_parent)] = valid_jids.index(jid)

                    ### 非右转选择
                    green_signal = np.array([1 if j == 2 and laneid2features[i].turn != 3 else 0 for i, j in zip(junction.lane_ids, phase.states)])    
                    in_lane = np.array([laneid2features[lane_id].predecessors[0].id for lane_id in junction.lane_ids])[green_signal==1].tolist()
                    out_lane = np.array([laneid2features[lane_id].successors[0].id for lane_id in junction.lane_ids])[green_signal==1].tolist()

                    in_lane_parents = set(np.array([laneid2features[lane_id].parent_id for lane_id in in_lane]).tolist())
                    out_lane_parents = set(np.array([laneid2features[lane_id].parent_id for lane_id in out_lane]).tolist())

                    for (in_lane_parent, out_lane_parent) in zip(in_lane_parents, out_lane_parents):
                        if (in_lane_parent, out_lane_parent) not in road2phase:
                            road2phase[(in_lane_parent, out_lane_parent)] = [0, 0, 0, 0]   
                            road2phase[(in_lane_parent, out_lane_parent)][phase_idx] = 1
                            road2junction[(in_lane_parent, out_lane_parent)] = valid_jids.index(jid)
            return road2phase, road2junction
        
        self.road2phase, self.road2junction = phase_road_encoder()

        if os.path.exists(f'{data_path}/selected_junction_ids.json'):
            jids = json.load(open(f'{data_path}/selected_junction_ids.json'))
        else:
            jids = list([junction.id for junction in M.junctions])
        
        print('#Junctions:', len(jids))

        self.num_junctions = len(jids)
        self.intention = args.intention
        jidxs = [self.junction_id2idxs[jid] for jid in jids]
        self.jids = jids
        self.jidxs = jidxs
        self.junction_action_sizes = [len(m) for m in self.phase_lanes]
        self.junction_available_actions = np.zeros((len(jidxs), self.max_junction_action_size), dtype=int)
        for i, jidx in enumerate(jidxs):
            self.junction_available_actions[i, :self.junction_action_sizes[i]] = 1
        self.junction_type_list = [np.array([(m==3)|(m==6), (m==4)|(m==8)]).astype(int) for m in self.junction_action_sizes]
        self.junction_scale_list = np.array([[len(i)/j] for i, j in zip(self.in_lanes, self.junction_action_sizes)])
            
        self.junction_phase_lanes_inflow, self.junction_phase_lanes_outflow, self.junction_zero_lanes_inflow, self.junction_zero_lanes_outflow, self.junction_missing_lanes_inflow, self.junction_missing_lanes_outflow, self.junction_non_zeros_inflow, self.junction_non_zeros_outflow, self.junction_missing_phase = junction_phase_lane_numpy(self.phase_lanes)
        self.junction_in_lanes, self.junction_in_lanes_zero = junction_in_lane_numpy(self.in_lanes)

        ## 初版道路路口性质：相位的分别的规模、相位数
        self.junction_phase_sizes = np.zeros((len(self.jids), max(self.junction_action_sizes) + 1))
        for i, phase in enumerate(self.phase_lanes):
            for j, lanes in enumerate(phase):
                self.junction_phase_sizes[i, j] = len(lanes[0])
            self.junction_phase_sizes[i, -1] = len(phase)
        
        self.road_length = np.array(road_length)

        def calculate_angle(x1, y1, x2, y2, x, y):
            dx1 = np.array(x2 - x1)
            dy1 = np.array(y2 - y1)
            dx2 = np.array(x - x2)
            dy2 = np.array(y - y2)
            theta_A_rad = math.degrees(math.atan2(dy1, dx1))
            if type(x) == np.ndarray:
                theta_B_rad = np.array([math.degrees(math.atan2(dy2[i], dx2[i])) for i in range(len(dx2))])
            else:
                theta_B_rad = math.degrees(math.atan2(dy2, dx2))
            angle = abs(np.array(theta_B_rad - theta_A_rad))
            return angle
        
        def calculate_direction(x1, y1, x2, y2):
            dx1 = np.array(x2 - x1)
            dy1 = np.array(y2 - y1)
            theta_A_rad = math.degrees(math.atan2(dy1, dx1))
            return theta_A_rad
        
        ### 以junction为点，road为边构图
        self.junctionid2idxs = {}
        self.junctionidx2id = {}
        for i, junction in enumerate(M.junctions):
            self.junctionid2idxs[junction.id] = i
            self.junctionidx2id[i] = junction.id

        junction2inroad = {}
        junction2outroad = {}
        for junction in M.junctions:
            in_road_ids = []
            out_road_ids = []
            for driving_lane_groups in junction.driving_lane_groups:
                in_road_ids.append(driving_lane_groups.in_road_id)
                out_road_ids.append(driving_lane_groups.out_road_id)
            junction2inroad[self.junctionid2idxs[junction.id]] = list(set(in_road_ids))
            junction2outroad[self.junctionid2idxs[junction.id]] = list(set(out_road_ids))
        
        road2injunction = {}
        road2outjunction = {}
        for junction in junction2inroad:
            for road in junction2inroad[junction]:
                if road not in road2injunction:
                    road2injunction[road] = [junction]
                else:
                    road2injunction[road].append(junction)
            for road in junction2outroad[junction]:
                if road not in road2outjunction:
                    road2outjunction[road] = [junction]
                else:
                    road2outjunction[road].append(junction)
        for road in road2injunction:
            road2injunction[road] = list(set(road2injunction[road]))
        for road in road2outjunction:
            road2outjunction[road] = list(set(road2outjunction[road]))

        ### 对路口构造边
        edge_set = []
        for road in M.roads:
            road_id = road.id
            source_junction = road2outjunction[road_id][0]
            target_junction = road2injunction[road_id][0]
            edge_set.append((source_junction, target_junction, self.road_length[self.road_id2idxs[road_id]]))

        G = nx.DiGraph()
        G.add_weighted_edges_from(edge_set)

        def calculate_direct_distance(x1, y1, x2, y2):
            dx = np.array(x2 - x1)
            dy = np.array(y2 - y1)
            direct_distance = np.sqrt(dx**2 + dy**2)
            return direct_distance
        
        # ### 提前存储所有路和所有终点之间的关系
        self.angle_matrix = np.zeros((len(M.roads), len(M.roads)))
        self.direction_matrix = np.zeros(len(M.roads))
        self.distance_matrix = np.ones((len(M.roads), len(M.roads)))*(-1000)
        self.direct_distance_matrix = np.zeros((len(M.roads), len(M.roads)))
        shortest_distance = {}
        for i, road in tqdm(enumerate(M.roads)):
            ### 
            self.angle_matrix[i, :] = calculate_angle(self.road_start_xs[i], self.road_start_ys[i], self.road_end_xs[i], self.road_end_ys[i], self.road_start_xs, self.road_start_ys)
            self.direction_matrix[i] = calculate_direction(self.road_start_xs[i], self.road_start_ys[i], self.road_end_xs[i], self.road_end_ys[i])
            for j, end_road in enumerate(M.roads):
                if i == j:
                    self.distance_matrix[i, j] = 0
                    continue
                self.distance_matrix[i, j] = nx.shortest_path_length(G, road2injunction[self.road_idxs2id[i]][0], road2outjunction[self.road_idxs2id[j]][0], weight='weight')
                shortest_path = nx.shortest_path(G, road2injunction[self.road_idxs2id[i]][0], road2outjunction[self.road_idxs2id[j]][0], weight='weight')
                shortest_path = [self.junctionidx2id[junction] for junction in shortest_path]
                shortest_distance[(road.id, end_road.id)] = shortest_path
                self.distance_matrix[i, j] += road_length[i]
                self.direct_distance_matrix[i, j] = calculate_direct_distance(self.road_start_xs[i], self.road_start_ys[i], self.road_start_xs[j], self.road_start_ys[j])

        self.angle_triplet_matrix = np.zeros((len(M.roads), len(M.roads), len(M.roads)))    ### 存储路B在从路A到路C中间的关系 是否是顺承关系
        for i, road in tqdm(enumerate(M.roads)):
            for j, end_road in enumerate(M.roads):
                if i == j:
                    continue
                for k, mid_road in enumerate(M.roads):
                    start_x, start_y = self.road_end_xs[i], self.road_end_ys[i]
                    mid_x, mid_y = self.road_start_xs[k], self.road_start_ys[k]
                    end_x, end_y = self.road_start_xs[j], self.road_start_ys[j]
                    self.angle_triplet_matrix[i, k, j] = calculate_angle(start_x, start_y, mid_x, mid_y, end_x, end_y)

        self.angle_distance_matrix = np.zeros((len(M.roads), len(M.roads), len(M.roads)))    ### 存储路B在从路A到路C中间的关系 去B是否绕开了角度
        for i, road in tqdm(enumerate(M.roads)):
            for j, end_road in enumerate(M.roads):
                for k, mid_road in enumerate(M.roads):
                    start_x, start_y = self.road_start_xs[i], self.road_start_ys[i]
                    mid_x, mid_y = self.road_start_xs[k], self.road_start_ys[k]
                    end_x, end_y = self.road_start_xs[j], self.road_start_ys[j]
                    self.angle_distance_matrix[i, k, j] = abs(calculate_direction(start_x, start_y, mid_x, mid_y) - calculate_direction(start_x, start_y, end_x, end_y))

        # np.savez_compressed(f'{data_path}/saved_matrix.npz', 
        #                     angle_matrix=self.angle_matrix,
        #                     direction_matrix=self.direction_matrix,
        #                     distance_matrix=self.distance_matrix,
        #                     direct_distance_matrix=self.direct_distance_matrix,
        #                     angle_triplet_matrix=self.angle_triplet_matrix,
        #                     angle_distance_matrix=self.angle_distance_matrix)

        # self.angle_matrix = np.load(f'{data_path}/saved_matrix.npz')['angle_matrix']
        # self.direction_matrix = np.load(f'{data_path}/saved_matrix.npz')['direction_matrix']
        # self.distance_matrix = np.load(f'{data_path}/saved_matrix.npz')['distance_matrix']
        # self.direct_distance_matrix = np.load(f'{data_path}/saved_matrix.npz')['direct_distance_matrix']
        # self.angle_triplet_matrix = np.load(f'{data_path}/saved_matrix.npz')['angle_triplet_matrix']
        # self.angle_distance_matrix = np.load(f'{data_path}/saved_matrix.npz')['angle_distance_matrix']

        ## 存储道路在不同时间步的属性
        self.road_states = np.zeros((self.step_count+1, len(M.roads), self.neighbor_state_dim))

        ### 保存一个
        self.road_adj_mask = np.zeros((len(M.roads), len(M.roads)))
        for road in road2adjroad:
            self.road_adj_mask[self.road_id2idxs[road], self.road_id2idxs[road]] = 1
            for adjroad in road2adjroad[road]:
                self.road_adj_mask[self.road_id2idxs[road], self.road_id2idxs[adjroad]] = 1
                for two_hop_adjroad in road2adjroad[adjroad]:
                    self.road_adj_mask[self.road_id2idxs[road], self.road_id2idxs[two_hop_adjroad]] = 1

        ### 对junction进行构图
        self.junction_edges, self.junction_edge_feats = [], []      ### self.junction_edges对应的是(out_node, in_node)
        self.junction_edge_list = [[] for _ in range(len(self.jids))]       ### 这里记录的是(in_node, out_node)
        for jidx, out_lanes in enumerate(self.out_lanes):
            for out_lane in out_lanes:
                ids = [jid for (jid, in_lanes) in enumerate(self.in_lanes) if out_lane in in_lanes]
                if len(ids) != 0:
                    if [jidx, ids[0]] not in self.junction_edges:
                        self.junction_edges.append([jidx, ids[0]])
                        self.junction_edge_feats.append([M.lanes[out_lane].length, 1])
                        self.junction_edge_list[ids[0]].append(jidx)
                    else:
                        idx = [i for i, value in enumerate(self.junction_edges) if value == [jidx, ids[0]]][0]
                        self.junction_edge_feats[idx][1] += 1
        self.max_junction_neighbor_num = max([len(i) for i in self.junction_edge_list])
        print('Max Junction Neighbor Number:', self.max_junction_neighbor_num)
        self.connect_matrix = np.zeros((len(self.jids), len(self.jids)))
        for i, idxs in enumerate(self.junction_edge_list):
            if len(idxs) == 0:         
                continue
            for j in idxs:
                self.connect_matrix[j, i] = 1/len(idxs)

        ### 设置手动调控目标信控
        self.eng.set_tl_policy_batch(self.jidxs, TlPolicy.MANUAL)

        self.junction_phase_relation = np.zeros((len(self.jids), self.max_junction_neighbor_num, 2), dtype=int)   ### one-hot type information
        self.junction_neighbor_type = np.zeros((len(self.jids), self.max_junction_neighbor_num), dtype=int)
        self.junction_neighbor_mask = np.zeros((len(self.jids), self.max_junction_action_size), dtype=int)

        for dst_idx in tqdm(range(len(self.jids))):
            src_idxs = self.junction_edge_list[dst_idx] 
            if len(src_idxs) == 0:
                continue
            self.junction_neighbor_mask[dst_idx, :len(src_idxs)] = 1
            dst_phase_lanes = self.phase_lanes[dst_idx]
            dst_in_lanes = self.in_lanes[dst_idx]
            for idx, src_idx in enumerate(src_idxs):
                src_phase_lanes = self.phase_lanes[src_idx]
                src_out_lanes = self.out_lanes[src_idx]
                for dst_phase_idx, dst_phase in enumerate(dst_phase_lanes):
                    if set(dst_phase[0])&set(src_out_lanes) == set():
                        continue
                    if len(src_phase_lanes) > 2:
                        if set(src_phase_lanes[2][1])&set(dst_in_lanes) != set() or set(src_phase_lanes[1][1])&set(dst_in_lanes) != set():
                            self.junction_phase_relation[dst_idx, idx, :] = [1, 0]
                        else:
                            self.junction_phase_relation[dst_idx, idx, :] = [0, 1]
                    elif len(src_phase_lanes) == 2:
                        if set(src_phase_lanes[1][1])&set(dst_in_lanes) != set():
                            self.junction_phase_relation[dst_idx, idx, :] = [1, 0]
                        else:
                            self.junction_phase_relation[dst_idx, idx, :] = [0, 1]
                    if dst_phase_idx in [0, 1]:
                        self.junction_neighbor_type[dst_idx, idx] = 0
                    else:
                        self.junction_neighbor_type[dst_idx, idx] = 1

        self.junction_edge_distance = np.zeros((len(self.jids), self.max_junction_neighbor_num, 1))
        self.junction_edge_strength = np.zeros((len(self.jids), self.max_junction_neighbor_num, 1))

        for i, idxs in enumerate(self.junction_edge_list):
            if len(idxs) == 0:
                continue
            self.junction_edge_distance[i, :len(idxs), :] = np.array([min(self.junction_edge_feats[self.junction_edges.index([j, i])][0], 500) for j in idxs]).reshape(-1, 1)    # 超过500米的邻居也认为是500米算
            self.junction_edge_strength[i, :len(idxs), :] = np.array([self.junction_edge_feats[self.junction_edges.index([j, i])][1] for j in idxs]).reshape(-1, 1)
        self.junction_edge_distance = self.junction_edge_distance/100
        self.junction_edge_distance = np.concatenate([self.junction_edge_distance, self.junction_edge_strength], axis=2)

        # ### 考虑neighbor_type排列邻居顺序
        self.junction_edge_list_rearraged = [[] for _ in range(len(self.jids))]
        self.junction_phase_relation_rearraged = np.zeros((len(self.jids), self.max_junction_neighbor_num, 2), dtype=int)
        self.junction_neighbor_type_rearraged = np.zeros((len(self.jids), self.max_junction_neighbor_num), dtype=int)
        self.junction_edge_distance_rearraged = np.zeros((len(self.jids), self.max_junction_neighbor_num, 2))
        self.junction_neighbor_mask_rearraged = np.zeros((len(self.jids), self.max_junction_neighbor_num), dtype=int)
                
        print('neighbor junction specification.')
        for dst_idx in tqdm(range(len(self.jids))):
            src_idxs = self.junction_edge_list[dst_idx]
            if len(src_idxs) == 0:
                continue
            src_idxs_new = np.zeros(4, dtype=int)
            idxs_new = np.zeros(4, dtype=int)
            typea_idxs = [i for i, j in enumerate(self.junction_neighbor_type[dst_idx, :len(src_idxs)]) if j == 0]
            src_idxs_new[:len(typea_idxs)] = [src_idxs[i] for i in typea_idxs]
            self.junction_neighbor_mask_rearraged[dst_idx, :len(typea_idxs)] = 1
            idxs_new[:len(typea_idxs)] = typea_idxs
            typeb_idxs = [i for i, j in enumerate(self.junction_neighbor_type[dst_idx, :len(src_idxs)]) if j == 1]
            src_idxs_new[2:2+len(typeb_idxs)] = [src_idxs[i] for i in typeb_idxs]
            self.junction_neighbor_mask_rearraged[dst_idx, 2:2+len(typeb_idxs)] = 1
            idxs_new[2:2+len(typeb_idxs)] = typeb_idxs
            self.junction_edge_list_rearraged[dst_idx] = src_idxs_new.tolist()
            self.junction_phase_relation_rearraged[dst_idx, :] = self.junction_phase_relation[dst_idx, idxs_new]
            self.junction_neighbor_type_rearraged[dst_idx, :] = self.junction_neighbor_type[dst_idx, idxs_new]
            self.junction_edge_distance_rearraged[dst_idx, :] = self.junction_edge_distance[dst_idx, idxs_new]

        self.action_mean_field_query_matrix = np.zeros((self.num_roads, self.num_roads, self.num_roads))
        for i, road in tqdm(enumerate(M.roads)):
            for j, dest in enumerate(M.roads):
                for k, mid_road in enumerate(M.roads):
                    direct_distance = self.direct_distance_matrix[i, k]
                    angle = self.angle_distance_matrix[i, k, j]
                    mid_road_angle = calculate_direction(self.road_start_xs[k], self.road_start_ys[k], self.road_end_xs[k], self.road_end_ys[k]) - calculate_direction(self.road_start_xs[k], self.road_start_ys[k], self.road_start_xs[j], self.road_start_ys[j])
                    self.action_mean_field_query_matrix[i, k, j] = np.exp(-(direct_distance/500)) * (1+np.cos(angle)) * (1+np.cos(mid_road_angle))      ### 距离衰减参数是个超参
        # np.savez_compressed(f'{data_path}/action_mean_field_query_matrix.npz', action_mean_field_query_matrix=self.action_mean_field_query_matrix)
        # self.action_mean_field_query_matrix = np.load(f'{data_path}/action_mean_field_query_matrix.npz')['action_mean_field_query_matrix']

        ### 初始states
        self.vehicle_actions = {}
        self.fresh_state()
        self.roadidx2corrroadidx = self.roadidx2adjroadidx

        self.reward_weight = reward_weight
        self.balancing_coef = args.balancing_coef
        self.emission_coef = args.emission_coef

        self.routing_queries = []
        self.agg = args.agg
        self.corr_agg = args.corr_agg
        self.lc_interval = args.lc_interval

        self.road_corr_adj_matrix = np.zeros((len(self.road_idxs2id), len(self.road_idxs2id)))
        if self.corr_agg == 1:
            self.road_corr_adj_matrix = np.zeros((len(self.road_idxs2id), len(self.road_idxs2id)))

        self.edge_attr_matrix = np.zeros((len(M.roads), len(M.roads), len(M.roads), 2))
        for idx in self.road_idxs2id:
            self.edge_attr_matrix[idx, :] = np.concatenate([self.angle_triplet_matrix[:, :, [idx]]/180, np.expand_dims(self.distance_matrix, axis=2)/1000], axis=2)

        ### 记录vehicles记录
        self.vehicle_actions_record = {}
        self.junction_actions_record = []

    def lc_edge_extract(self):
        road_count = torch.tensor(self.road_states[:self._step, :, 0]).T        ### num_roads*step_count
        road_ffn = torch.fft.rfft(road_count, norm='ortho', dim=-1)     ### num_roads*step_count//2+1

        road_ffn = torch.cat([road_ffn.real, road_ffn.imag], dim=-1)        ### num_roads*(step_count+2)

        sim = road_ffn @ road_ffn.T
        sim = sim / (torch.norm(road_ffn, dim=-1).unsqueeze(-1) @ torch.norm(road_ffn, dim=-1).unsqueeze(0))        ### num_roads * num_roads

        ### 将2-hop内邻居mask掉
        sim[self.road_adj_mask==1] = -1e999

        ### 找到topk邻居
        topk = 4
        _, topk_idx = torch.topk(sim, topk, dim=-1)
        topk_idx = topk_idx.cpu().numpy()

        ### 存储dynamic edge
        self.road2corrroad = {}  
        self.roadidx2corrroadidx = {}
        for i, road in enumerate(self.road_idxs2id):
            self.road2corrroad[road] = [self.road_idxs2id[j] for j in topk_idx[i]]
            self.roadidx2corrroadidx[i] = topk_idx[i].tolist()

        ### 转换成corr adj matrix
        self.road_corr_adj_matrix = np.zeros((len(self.road_idxs2id), len(self.road_idxs2id)))
        for (road, corrroad) in self.roadidx2corrroadidx.items():
            self.road_corr_adj_matrix[road, corrroad] = 1

    def add_env_vc(self, vid, road, time, destination):
        self.vehicles[vid]={
            "destination": destination,
            "start_time":time,
            "time": time,
            "road":road,
            "next_road":None,
            "reward":None,
            "state":None,
            "is_new":True,
            'done':False,
            'last_reward':False,
            'last_road': None,
            'emissions': 0
            }

    def reset(self):
        self.eng.restore_checkpoint(self._cid)
        self.vehicles = {}
        self.success_travel_time, self.success_travel = 0, 0
        self.total_travel_time, self.total_travel = 0, 0
        self.total_emissions = 0

    def get_vehicle_distances(self):
        info = self.eng.fetch_persons()
        xs, ys = info['x'], info['y']
        lane_xs, lane_ys = self.lane_start_xs[info['lane_id'].tolist()], self.lane_start_ys[info['lane_id'].tolist()]

        distances = np.sqrt((xs - lane_xs)**2 + (ys - lane_ys)**2)
        return distances
    
    def get_routing_demand_ids(self):
        m = self.get_vehicle_distances()   
        n = self.eng.fetch_persons()
        m[(n['status']!=2)|(n['lane_parent_id']>=300000000)] = 1e999
        m[self.selected_person_masks==0] = 1e999
        roads = n['lane_parent_id'][m<60]
        ids = n['id'][m<60]
        idxs = self.veh_id2idxs[ids]

        routing_types = [[] for _ in range(2)]  # 0: 需要转向 1: 到达终点
        for veh_id, veh_idx, road in zip(ids, idxs, roads):
            if self.vehicles[veh_id]['last_reward'] is True:
                continue
            if (self.vehicles[veh_id]['next_road'] is not None) and road != self.vehicles[veh_id]['next_road']:  
                continue
            if self.vehicles[veh_id]['destination'] == road:   # 第一次到达终点道路
                routing_types[1].append([veh_id, veh_idx, road])
            else:
                routing_types[0].append([veh_id, veh_idx, road])
        return routing_types
    
    def reach_dest_detect(self, road, dest):
        if dest in self.road2adjroad[road]:
            return True
        return False

    def insert_next_road(self, veh, dest, action=None):
        road = self.eng.fetch_persons()['lane_parent_id'][self.veh_id2idxs[veh]]

        self.vehicle_actions_record[veh].append(action)
        action = self.road2adjroad[road][action]
        if action != dest:
            self.eng.set_vehicle_route(veh, [road, action, dest])
            self.vehicle_actions[veh] = (road, action)
        else:
            self.eng.set_vehicle_route(veh, [road, dest])
            self.vehicle_actions[veh] = (road, dest)
        self.vehicles[veh]['next_road'] = action
        self.vehicles[veh]['last_road'] = road
    
    def update_env_vc_info(self,vc,step,road,state, current_emission, done=False, first_decision=False, last_reward=False):	 	 
        ### 更新此时决策的信息
        self.vehicles[vc]["time"]=step
        self.vehicles[vc]["road"]=road
        self.vehicles[vc]["state"]=state
        self.vehicles[vc]["is_new"]=False
        self.vehicles[vc]['done']=done
        self.vehicles[vc]['emissions']=current_emission
        self.vehicles[vc]['first_decision']=first_decision
        self.vehicles[vc]['last_reward']=last_reward
    
    def get_new_vehs(self, step):
        ### 查找在step和step-1之间出发的agent
        person_info = self.eng.fetch_persons()
        departure_times = person_info['departure_time']
        ids = person_info['id'][(departure_times>=(step-1))&(departure_times<step)]
        roads = person_info['lane_parent_id'][(departure_times>=(step-1))&(departure_times<step)]
        departure_times = person_info['departure_time'][(departure_times>=(step-1))&(departure_times<step)]
        selected_ids = [id in self.selected_person_ids for id in ids]
        ids = ids[selected_ids]
        roads = roads[selected_ids]
        departure_times = departure_times[selected_ids]        
        for id in ids:
            self.vehicle_actions_record[id] = []
        return ids, roads, departure_times
    
    def success_routing(self, vc, emission, timeout=False):
        self.success_travel_time += self._step-self.vehicles[vc]['start_time'] if not timeout else 0
        self.total_travel_time += self._step-self.vehicles[vc]['start_time']
        self.success_travel += 1 if not timeout else 0
        self.total_travel += 1
        self.total_emissions += emission
        if vc in self.vehicle_actions:
            del self.vehicle_actions[vc]

    def lane_vehicle_cal(self):
        speed_threshold = 0.1
        vehicle_counts = np.zeros(len(self.lane_id2idxs))
        vehicle_waiting_counts = np.zeros(len(self.lane_id2idxs))
        for lane, status, v, lane_parent_id in zip(self.eng.fetch_persons()['lane_id'], self.eng.fetch_persons()['status'], self.eng.fetch_persons()['v'], self.eng.fetch_persons()['lane_parent_id']):
            if status == 2 and lane_parent_id < 300000000:
                vehicle_counts[self.lane_id2idxs[lane]] += 1
                if v < speed_threshold:
                    vehicle_waiting_counts[self.lane_id2idxs[lane]] += 1
        return vehicle_counts, vehicle_waiting_counts
    
    # def road_speed_cal(self, road_vehicle_count): ### 可以直接通过fetch_lanes获取speed
    #     road_speed_overall = np.zeros(len(self.road_id2idxs))
    #     for status, v, road in zip(self.eng.fetch_persons()['status'], self.eng.fetch_persons()['v'], self.eng.fetch_persons()['lane_parent_id']):
    #         if status == 2 and road < 300000000:
    #             road_speed_overall[self.road_id2idxs[road]] += v
    #     road_speed_ave = road_speed_overall/road_vehicle_count
    #     road_travel_time_ave = self.road_length/road_speed_ave

    #     # 将nan值设为-1
    #     road_speed_ave[np.isnan(road_speed_ave)] = -1
    #     road_travel_time_ave[np.isnan(road_travel_time_ave)] = -1

    #     ### 将travel_time_ave为np.inf的值设为-1
    #     road_travel_time_ave[np.isinf(road_travel_time_ave)] = -1
    #     return road_speed_ave, road_travel_time_ave
    
    def action_mean_field_cal(self, action, dest):
        road = self.eng.fetch_persons()['lane_parent_id'][self.veh_id2idxs[dest]]
        action_mean_field = np.zeros((self.num_roads, 1))
        if action != dest:
            action_mean_field[self.road_id2idxs[road], action] += 1
        else:
            action_mean_field[self.road_id2idxs[road], dest] += 1
        return action_mean_field

    def fresh_state(self):  ### 可以考虑通过fetch_lanes加入speed
        lane_vehicle_counts, lane_vehicle_waiting_counts = self.lane_vehicle_cal()
        lane_speeds = self.eng.fetch_lanes()['v_avg']
        road_vehicle = lane_vehicle_counts[self.lanes_list]
        road_vehicle[self.lanes_zero==1]==0
        road_vehicle = np.sum(road_vehicle, axis=1)

        road_waiting_vehicle = lane_vehicle_waiting_counts[self.lanes_list]
        road_waiting_vehicle[self.lanes_zero==1]==0
        road_waiting_vehicle = np.sum(road_waiting_vehicle, axis=1)

        road_vehicle_density = road_vehicle/self.road_length
        road_waiting_vehicle_density = road_waiting_vehicle/self.road_length
        road_length = self.road_length
        road_direction = self.direction_matrix/180

        road_speeds = lane_speeds[self.lanes_list]
        road_speeds[self.lanes_zero==1] = 0
        road_speeds = np.mean(road_speeds, axis=1)/40

        ### 获取动作平均场
        self.action_mean_field = np.zeros((1, self.num_roads))
        for vc, (road, action) in self.vehicle_actions.items():
            dest = self.veh2dest[vc]
            self.action_mean_field += self.action_mean_field_query_matrix[self.road_id2idxs[action], :, self.road_id2idxs[dest]].reshape(1, -1)

        self.road_state = np.vstack([road_vehicle_density, road_waiting_vehicle_density, road_length, road_direction, road_speeds, self.action_mean_field/100]).T
        self.road_states[self._step] = self.road_state

    ### 获取临近道路的静态特征
    def get_state(self, road, destination):
        state = np.zeros((self.max_action_size*self.source_state_dim+1))
            
        adj_roadidxs = self.roadidx2adjroadidx[self.road_id2idxs[road]]
        # road_angle = self.angle_matrix[adj_roadidxs, self.road_id2idxs[destination]]        ### angle_matrix要重新定义
        road_angle = self.direction_matrix[adj_roadidxs]/180
        road_distance = self.distance_matrix[adj_roadidxs, self.road_id2idxs[destination]]
        road_length = self.road_length[adj_roadidxs]
        state[:len(road_angle)] = road_distance/1000  
        state[self.max_action_size*1:self.max_action_size*1+len(road_distance)] = road_length/100   
        state[self.max_action_size*2:self.max_action_size*2+len(road_angle)] = road_angle/180
        state[-1] = self._step/self.step_count

        return state, self.road_id2idxs[destination]
    
    def junction_observe(self):
        _, lane_vehicle_waiting_counts = self.lane_vehicle_cal()
        in_cnt_states = lane_vehicle_waiting_counts[self.junction_phase_lanes_inflow]
        in_cnt_states[self.junction_zero_lanes_inflow==1] = 0
        in_cnt_states[self.junction_missing_lanes_inflow==1] = -1
        in_cnt_states = np.sum(in_cnt_states, axis=2)
        in_cnt_states[self.junction_missing_phase==1] = -1
        
        if self.intention == 1:
            ### 加入车辆转向意图观察
            self.junction_actions = np.zeros((len(self.junction_phase_sizes), self.max_junction_action_size))
            for veh, (road, action) in self.vehicle_actions.items():
                if (road, action) not in self.road2junction:
                    continue
                self.junction_actions[self.road2junction[(road, action)]] += self.road2phase[(road, action)]
            observe_states = np.concatenate([in_cnt_states, self.junction_actions, self.junction_phase_sizes], axis=1)
        else:
            observe_states = np.concatenate([in_cnt_states, self.junction_phase_sizes], axis=1)
        return observe_states

    def extra_reward(self, vc, last_road, next_road):
        last_distance = self.distance_matrix[self.road_id2idxs[last_road], self.road_id2idxs[self.vehicles[vc]['destination']]]
        next_distance = self.distance_matrix[self.road_id2idxs[next_road], self.road_id2idxs[self.vehicles[vc]['destination']]]
        reward = self.reward_weight*(last_distance - next_distance)/1000
        return reward

    def step(self, actions=None, junction_actions=None):
        new_experiences = {'action_side':{}, 'obs_side':{}}
        junction_experiences = {'junction_states': None, 'junction_rewards': None, 'junction_dones': None, 'junction_actions': None}
        if len(actions) > 0 and self._step < self.step_count:
            for (veh, dest), action in zip(self.routing_queries, actions):  
                self.insert_next_road(veh, dest, action)      
                new_experience = {veh: {'action_signal': 1, 'action': action}}
                new_experiences['action_side'].update(new_experience)
            
        if len(junction_actions) > 0:
            self.junction_actions_record.append(junction_actions)
            self.eng.set_tl_phase_batch(self.jidxs, junction_actions)
            self.one_hot_action_matrix = self.one_hot_mapping_matrix[np.array(junction_actions).reshape(-1), :]
            junction_experiences = {'junction_states': None, 'junction_rewards': None, 'junction_dones': None, 'junction_actions': junction_actions}

        self.eng.next_step(1)
        self._step = self._step + 1
        if self.record:
            self.recorder.record()

        if self.corr_agg == 1:
            if self._step % self.lc_interval == 0:
                self.lc_edge_extract()

        junction_states, junction_reward = None, None
        junction_done = 0
        self.info['junction_reward'] = None
        ### 信控更新
        if self._step % self.tl_interval == 0:
            junction_states = self.junction_observe()
            junction_states = np.concatenate([junction_states, self.one_hot_action_matrix], axis=1)
        
            ### 信控reward
            _, lane_vehicle_waiting_counts = self.lane_vehicle_cal()
            if self.junction_reward == 'queue':
                junction_reward = lane_vehicle_waiting_counts[self.in_lane_array]
                junction_reward[self.zero_in_lane_array==1] = 0
                junction_reward = -np.sum(junction_reward, axis=1)
            elif self.junction_reward == 'one_hop_queue':
                junction_reward = lane_vehicle_waiting_counts[self.in_lane_array]
                junction_reward[self.zero_in_lane_array==1] = 0
                junction_reward = -np.sum(junction_reward, axis=1)
                junction_reward = np.dot(junction_reward, self.connect_matrix)*self.alpha + junction_reward
            else:
                raise ValueError("Invalid junction reward type")
            
            if self._step == self.step_count:
                junction_done = 1 
                junction_experiences = {'junction_states': junction_states, 'junction_rewards': junction_reward, 'junction_dones': junction_done, 'junction_actions': None}
            else:
                junction_experiences = {'junction_states': junction_states, 'junction_rewards': junction_reward, 'junction_dones': junction_done, 'junction_actions': None}
            self.info['junction_reward'] = np.mean(junction_reward)

        vehs, roads, departure_times = self.get_new_vehs(self._step)
        for veh, road, departure_time in zip(vehs, roads, departure_times):
            destination = self.veh2dest[veh]  
            self.add_env_vc(veh, road, departure_time, destination)

        self.routing_queries = []

        [routing_demands, finish_demands] = self.get_routing_demand_ids()     
        if len(routing_demands) > 0 or len(finish_demands) > 0:
            self.fresh_state()    
        
        current_emissions = self.eng.fetch_persons()['cum_co2']

        next_veh, success_veh = [], []
        all_rewards, time_rewards, distance_rewards, emission_rewards = 0, 0, 0, 0
        next_states, next_acs, next_all_states, next_ridxs, next_corr_adj_matrice, dest_idxs = [], [], [], [], [], []
        
        self.info['reward'] = 0
        self.info['time_reward'] = 0
        self.info['distance_reward'] = 0
        self.info['emission_reward'] = 0

        ## 日常决策
        for (vc, vidx, road) in routing_demands:
            if self._step < self.step_count: 
                next_state, dest_idx = self.get_state(road, self.vehicles[vc]['destination'])
                next_all_state = self.road_states[self._step]      ### 所有道路的当前特征
                available_action = self.available_actions[road]
                next_veh.append(vc)
                if self.vehicles[vc]['is_new']:
                    reward = None
                    self.update_env_vc_info(vc, self._step, road, next_state[-1], current_emissions[vidx], first_decision=True)
                else:
                    if self.vehicles[vc]['first_decision']:
                        reward = None
                    else:
                        distance_reward = self.extra_reward(vc, self.vehicles[vc]['last_road'], self.vehicles[vc]['next_road'])
                        time_reward = -(self._step-self.vehicles[vc]['time']) / 100
                        emission_reward = -(current_emissions[vidx] - self.vehicles[vc]['emissions'])   # g
                        if self.reward == 'only_distance':
                            reward = distance_reward
                        elif self.reward == 'distance':
                            reward = time_reward + distance_reward*self.balancing_coef
                        elif self.reward == 'emission':
                            reward = time_reward + distance_reward*self.balancing_coef + emission_reward*self.emission_coef
                        else:
                            reward = time_reward
                        all_rewards += time_reward + distance_reward*self.balancing_coef + emission_reward*self.emission_coef
                        time_rewards += time_reward
                        distance_rewards += distance_reward
                        emission_rewards += emission_reward
                    self.update_env_vc_info(vc, self._step, road, next_state[-1], current_emissions[vidx])
                self.routing_queries.append((vc, self.vehicles[vc]['destination']))
                new_experience = {vc: {'next_state': next_state, 'next_all_state': next_all_state, 'ridx': self.road_id2idxs[road], 'dest_idx': dest_idx, 'available_action': available_action, 'reward': reward, 'success': 0, 'timeout': 0, 'action_signal': 0, 'lc_edge_adj': self.road_corr_adj_matrix}}
                new_experiences['obs_side'].update(new_experience)
                next_states.append(next_state)
                next_all_states.append(next_all_state)
                next_acs.append(available_action)
                dest_idxs.append(dest_idx)
                next_ridxs.append(self.road_id2idxs[road])
                next_corr_adj_matrice.append(self.road_corr_adj_matrix)

        assert len(next_veh) == len(self.routing_queries) == len(next_states) == len(next_acs) == len(next_corr_adj_matrice)

        for (vc, vidx, road) in finish_demands:  
            next_state, dest_idx = self.get_state(road, self.vehicles[vc]["destination"])
            next_all_state = self.road_states[self._step]
            success_veh.append(vc)
            if self.vehicles[vc]['first_decision']:
                reward = None
                new_experience = {vc: {'next_state': next_state, 'next_all_state':next_all_state, 'ridx': self.road_id2idxs[road], 'dest_idx': dest_idx, 'available_action': None, 'reward': [10], 'success': 1, 'timeout': 0, 'action_signal': 0, 'lc_edge_adj': self.road_corr_adj_matrix}}
            else:
                distance_reward = self.extra_reward(vc, self.vehicles[vc]['last_road'], self.vehicles[vc]['next_road'])
                time_reward = -(self._step-self.vehicles[vc]['time']) / 100
                emission_reward = -(current_emissions[vidx] - self.vehicles[vc]['emissions']) /1e4   # g
                if self.reward == 'only_distance':
                    reward = distance_reward
                elif self.reward == 'distance':
                    reward = time_reward + distance_reward*self.balancing_coef
                elif self.reward == 'emission':
                    reward = time_reward + distance_reward*self.balancing_coef + emission_reward*self.emission_coef
                else:
                    reward = time_reward
                new_experience = {vc: {'next_state': next_state, 'next_all_state':next_all_state,  'dest_idx': dest_idx,  'ridx': self.road_id2idxs[road], 'available_action': None, 'reward': [reward, 10], 'success': 1, 'timeout': 0, 'action_signal': 0, 'lc_edge_adj': self.road_corr_adj_matrix}}
                all_rewards += reward + 10
                time_rewards += time_reward
                distance_rewards += distance_reward
                emission_rewards += emission_reward
            new_experiences['obs_side'].update(new_experience)
            self.update_env_vc_info(vc, self._step, road, next_state[-1], current_emissions[vidx], done=True, last_reward=True)
            self.success_routing(vc, current_emissions[vidx])

        if self.record:
            if self._step % 10 == 0:
                self.recorder.flush()

        if self._step >= self.step_count:
            junction_done = True
            for vc, _ in self.vehicles.items():
                vidx = self.veh_id2idxs[vc]
                if self.vehicles[vc]['is_new']:
                    self.success_routing(vc, current_emissions[vidx], timeout=True)
                    pass
                elif self.vehicles[vc]['first_decision'] and self.vehicles[vc]['last_reward'] is False:
                    new_experience = {vc: {'next_state': None, 'next_all_state':None, 'dest_idx': None, 'ridx': None, 'available_action': None, 'reward': [0], 'success': 0, 'timeout': 1, 'action_signal': 0, 'lc_edge_adj': None}}
                    new_experiences['obs_side'].update(new_experience)
                    self.success_routing(vc, current_emissions[vidx], timeout=True)
                    pass
                else:
                    if self.vehicles[vc]['last_reward'] is not True:
                        distance_reward = self.extra_reward(vc, self.vehicles[vc]['last_road'], self.vehicles[vc]['next_road'])
                        time_reward = -(self._step-self.vehicles[vc]['time']) / 100
                        emission_reward = -(current_emissions[vidx] - self.vehicles[vc]['emissions'])   # g
                        if self.reward == 'only_distance':
                            r = distance_reward
                        elif self.reward == 'distance':
                            r = time_reward + distance_reward*self.balancing_coef
                        elif self.reward == 'emission':
                            r = time_reward + distance_reward*self.balancing_coef + emission_reward*self.emission_coef
                        else:
                            r = time_reward
                        reward = [r, -10]
                        new_experience = {vc: {'next_state': None, 'next_all_state':None, 'dest_idx': None, 'ridx': None, 'available_action': None, 'reward': reward, 'success': 0, 'timeout': 1, 'action_signal': 0, 'lc_edge_adj': None}}
                        new_experiences['obs_side'].update(new_experience)
                        all_rewards += r - 10
                        time_rewards += time_reward
                        distance_rewards += distance_reward
                        emission_rewards += emission_reward
                        self.success_routing(vc, current_emissions[vidx], timeout=True)

            self.info['ATT_success'] = self.success_travel_time/self.success_travel if self.success_travel != 0 else 0
            self.info['ATT'] = self.total_travel_time/self.total_travel
            self.info['Throughput'] = self.success_travel
            self.info['emissions'] = self.total_emissions
            self.info['VEH'] = self.total_travel
            self._step = 0
            self.reset()

            if self.record:
                exit()
        
        self.info['rewards'] = all_rewards
        self.info['time_reward'] = time_rewards
        self.info['distance_reward'] = distance_rewards
        self.info['emission_reward'] = emission_rewards
        info = self.info
        return new_experiences, next_veh, success_veh, info, next_states, next_all_states, dest_idxs, next_ridxs, next_acs, next_corr_adj_matrice, junction_states, junction_experiences
    
def save(model, j_model, save_dir, type='present'):
    torch.save(model.state_dict(), str(save_dir) + "/{}.pt".format(type))
    torch.save(j_model.state_dict(), str(save_dir) + "/{}_junctions.pt".format(type))

def load(trainer, load_dir):
    trainer.policy.actor.load_state_dict(torch.load(str(load_dir) + "/actor_present.pt"))
    trainer.policy.critic.load_state_dict(torch.load(str(load_dir) + "/critic_present.pt"))
    print('Load Pretrained!')


class Replay_tmp:
    def __init__(self, max_size):
        self.ob = deque([], max_size)
        self.action = deque([], max_size)
        self.available_action = deque([], max_size)
        self.reward = deque([], max_size)
        self.done = deque([], max_size)
        self.ob_all = deque([], max_size)
        self.dest_idxs = deque([], max_size)
        self.ridxs = deque([], max_size)
        self.corr_adj_matrix = deque([], max_size)

    def pop(self):
        ob = self.ob.popleft()
        action = self.action.popleft()
        available_action = self.available_action.popleft()
        reward = self.reward.popleft()
        done = self.done.popleft()
        ob_all = self.ob_all.popleft()
        dest_idxs = self.dest_idxs.popleft()
        ridx = self.ridxs.popleft()
        corr_adj_matrix = self.corr_adj_matrix.popleft()
        next_ob = self.ob[0]
        next_ob_all = self.ob_all[0]
        next_dest_idxs = self.dest_idxs[0]
        next_corr_adj_matrix = self.corr_adj_matrix[0]
        return [ob, action, available_action, reward, done, ob_all, dest_idxs, ridx, corr_adj_matrix, next_ob, next_ob_all, next_dest_idxs, next_corr_adj_matrix]

class ReplayBuffer:
    def __init__(self, max_size):
        self.max_size = max_size
        self.replay = deque([], max_size)
        self.replay_tmp = defaultdict(lambda: Replay_tmp(max_size)) 

    def len(self):
        return len(self.replay)

    def add(self, experiences):
        self.replay.extend(experiences)

    def sample(self, batchsize, transpose=False):
        s, a, ac, r, d, sa, didxs, ridxs, cam, next_s, next_sa, next_didxs, next_cam = zip(*random.sample(self.replay, min(len(self.replay), batchsize)))
        if transpose:
            s, a, ac, r, sa, didxs, ridxs, cam, next_s, next_sa, next_didxs, next_cam = (list(zip(*i)) for i in [s, a, ac, r, sa, didxs, ridxs, cam, next_s, next_sa, next_didxs, next_cam])
        return s, a, ac, r, d, sa, didxs, ridxs, cam, next_s, next_sa, next_didxs, next_cam

    def add_tmp(self, experiences):     ### 给每个临时experience加观察
        timeout = 0
        for veh in experiences['action_side']:
            if experiences['action_side'][veh]['action_signal'] == 1:
                self.replay_tmp[veh].action.append(experiences['action_side'][veh]['action'])
                assert len(self.replay_tmp[veh].action) == len(self.replay_tmp[veh].ob)
                 
        for veh in experiences['obs_side']:
            if experiences['obs_side'][veh]['success'] != 1 and experiences['obs_side'][veh]['timeout'] != 1:
                self.replay_tmp[veh].ob.append(experiences['obs_side'][veh]['next_state'])
                self.replay_tmp[veh].available_action.append(experiences['obs_side'][veh]['available_action'])
                self.replay_tmp[veh].done.append(0)
                if experiences['obs_side'][veh]['reward'] is not None:
                    self.replay_tmp[veh].reward.append(experiences['obs_side'][veh]['reward'])
                self.replay_tmp[veh].ob_all.append(experiences['obs_side'][veh]['next_all_state'])
                self.replay_tmp[veh].dest_idxs.append(experiences['obs_side'][veh]['dest_idx'])
                self.replay_tmp[veh].ridxs.append(experiences['obs_side'][veh]['ridx'])
                self.replay_tmp[veh].corr_adj_matrix.append(experiences['obs_side'][veh]['lc_edge_adj'])
            elif experiences['obs_side'][veh]['success'] == 1:
                self.replay_tmp[veh].ob.append(experiences['obs_side'][veh]['next_state'])
                self.replay_tmp[veh].done[-1] = 1
                for reward in experiences['obs_side'][veh]['reward']:
                    self.replay_tmp[veh].reward.append(reward)
                self.replay_tmp[veh].ob_all.append(experiences['obs_side'][veh]['next_all_state'])
                self.replay_tmp[veh].dest_idxs.append(experiences['obs_side'][veh]['dest_idx'])
                self.replay_tmp[veh].ridxs.append(experiences['obs_side'][veh]['ridx'])
                self.replay_tmp[veh].corr_adj_matrix.append(experiences['obs_side'][veh]['lc_edge_adj'])
            else:
                timeout = 1
                self.replay_tmp[veh].ob.append(np.full_like(self.replay_tmp[veh].ob[-1], -1))
                self.replay_tmp[veh].ob_all.append(np.full_like(self.replay_tmp[veh].ob_all[-1], -1))
                self.replay_tmp[veh].dest_idxs.append(np.full_like(self.replay_tmp[veh].dest_idxs[-1], -1))
                self.replay_tmp[veh].ridxs.append(np.full_like(self.replay_tmp[veh].ridxs[-1], -1))
                self.replay_tmp[veh].corr_adj_matrix.append(np.full_like(self.replay_tmp[veh].corr_adj_matrix[-1], -1))
                for reward in experiences['obs_side'][veh]['reward']:
                    self.replay_tmp[veh].reward.append(reward)
                if len(self.replay_tmp[veh].ob) - 1 == len(self.replay_tmp[veh].reward) == len(self.replay_tmp[veh].done) == len(self.replay_tmp[veh].action) == len(self.replay_tmp[veh].available_action):
                    pass
                else:
                    print(veh, len(self.replay_tmp[veh].reward), len(self.replay_tmp[veh].ob), len(self.replay_tmp[veh].done), len(self.replay_tmp[veh].action), len(self.replay_tmp[veh].available_action))
                self.replay_tmp[veh].done[-1] = 1

        agg_experience = []
        for veh in self.replay_tmp:
            for i in range(len(self.replay_tmp[veh].reward)):
                agg_experience.append(self.replay_tmp[veh].pop())
        
        self.add(agg_experience)

        if timeout:
            self.replay_tmp = defaultdict(lambda: Replay_tmp(self.max_size))      
        return len(agg_experience)
    
class J_Replay_tmp:
    def __init__(self, max_size, junction_available_actions, phase_relation=None, neighbor_type=None, neighbor_idxs=None, neighbor_distances=None, neighbor_masks=None):
        self.max_size = max_size
        self.obs = deque([], max_size)
        self.actions = deque([], max_size)
        self.available_actions = deque([], max_size)
        self.rewards = deque([], max_size)
        self.dones = deque([], max_size)
        self.junction_available_actions = junction_available_actions
        self.neighbor_obs = deque([], max_size)
        self.neighbor_mask_replay = deque([], max_size)
        self.neighbor_type_replay = deque([], max_size)
        self.neighbor_relation_replay = deque([], max_size)
        self.neighbor_distances_replay = deque([], max_size)
        self.phase_relation = phase_relation
        self.neighbor_idxs = neighbor_idxs
        self.neighbor_masks = neighbor_masks
        self.neighbor_type = neighbor_type
        self.neighbor_distances = neighbor_distances

    def pop(self):
        return self.obs.popleft(), self.actions.popleft(), self.available_actions.popleft(), self.rewards.popleft(), self.dones.popleft(), \
            self.neighbor_obs.popleft(), self.neighbor_mask_replay.popleft(), self.neighbor_type_replay.popleft(), self.neighbor_relation_replay.popleft(), self.neighbor_distances_replay.popleft()

class J_ReplayBuffer:
    def __init__(self, max_size, num_agents, junction_available_actions, phase_relation=None, neighbor_type=None, neighbor_idxs=None, neighbor_distances=None, neighbor_masks=None):
        self.max_size = max_size
        self.junction_available_actions = junction_available_actions
        self.num_agents = num_agents
        self.max_num_neighbors = max([len(i) for i in neighbor_idxs])
        self.neighbor_idxs = neighbor_idxs
        self.neighbor_masks = neighbor_masks
        self.neighbor_type = neighbor_type
        self.neighbor_distances = neighbor_distances
        self.phase_relation = phase_relation
        self.replay = deque([], max_size)
        self.replay_tmp = J_Replay_tmp(max_size, junction_available_actions, phase_relation, neighbor_type, neighbor_idxs, neighbor_distances, neighbor_masks)
 
    def len(self):
        return len(self.replay)
    
    def cal_neighbor_obs(self, obs):
        obs = np.array(obs)
        neighbor_obs = np.zeros((obs.shape[0], self.max_num_neighbors, obs.shape[1]))
        neighbor_mask = np.zeros((obs.shape[0], self.max_num_neighbors))
        neighbor_type = np.zeros((obs.shape[0], self.max_num_neighbors))
        neighbor_relation = np.zeros((obs.shape[0], self.max_num_neighbors, 2))
        neighbor_distance = np.zeros((obs.shape[0], self.max_num_neighbors, 2))
        if self.neighbor_idxs is not None:
            for i, idxs in enumerate(self.neighbor_idxs):
                if idxs == []:
                    continue
                neighbor_obs[i, :len(idxs), :] = obs[idxs, :]
                neighbor_mask[i, :] = self.neighbor_masks[i, :]
                neighbor_type[i, :] = self.neighbor_type[i, :]
                neighbor_relation[i, :] = self.phase_relation[i, :]
                neighbor_distance[i, :] = self.neighbor_distances[i, :]
            self.replay_tmp.neighbor_obs.append(neighbor_obs)
            self.replay_tmp.neighbor_mask_replay.append(neighbor_mask)
            self.replay_tmp.neighbor_type_replay.append(neighbor_type)
            self.replay_tmp.neighbor_relation_replay.append(neighbor_relation)
            self.replay_tmp.neighbor_distances_replay.append(neighbor_distance)

    def add_tmp(self, junction_experiences):
        if junction_experiences['junction_actions'] is not None:
            self.replay_tmp.actions.append(junction_experiences['junction_actions'])
        if junction_experiences['junction_states'] is not None:
            self.replay_tmp.obs.append(junction_experiences['junction_states'])
            self.cal_neighbor_obs(junction_experiences['junction_states'])
            if junction_experiences['junction_dones'] is not None:
                self.replay_tmp.available_actions.append(self.junction_available_actions)
        if junction_experiences['junction_rewards'] is not None:
            self.replay_tmp.rewards.append(junction_experiences['junction_rewards'])
        if junction_experiences['junction_dones'] is not None:
            self.replay_tmp.dones.append([junction_experiences['junction_dones']]*self.num_agents)

        while len(self.replay_tmp.rewards) > 0:
            obs, actions, available_actions, rewards, dones, neighbor_obs, neighbor_mask_replay, neighbor_type_replay, neighbor_relation_replay, neighbor_distances_replay = \
                self.replay_tmp.pop()
            next_obs, next_neighbor_obs, next_neighbor_mask, next_neighbor_type_replay, next_neighbor_relation_replay, next_neighbor_distances_replay = \
            obs[1:], neighbor_obs[1:], neighbor_mask_replay[1:], neighbor_type_replay[1:], neighbor_relation_replay[1:], neighbor_distances_replay[1:]
            obs, neighbor_obs, neighbor_mask_replay, neighbor_type_replay, neighbor_relation_replay, neighbor_distances_replay = \
                obs[:-1], neighbor_obs[:-1], neighbor_mask_replay[:-1], neighbor_type_replay[:-1], neighbor_relation_replay[:-1], neighbor_distances_replay[:-1]
            for (ob, action, reward, next_ob, done, available_action, neighbor_ob, neighbor_mask, neighbor_type, neighbor_relation, neighbor_distance, next_neighbor_ob, next_neighbor_mask, next_neighbor_type, next_neighbor_relation, next_neighbor_distance) in \
                zip(obs, actions, rewards, next_obs, dones, available_actions, neighbor_obs, neighbor_mask_replay, neighbor_type_replay, neighbor_relation_replay, \
                    neighbor_distances_replay, next_neighbor_obs, next_neighbor_mask, next_neighbor_type_replay, next_neighbor_relation_replay, next_neighbor_distances_replay):
                self.replay.append([ob, action, available_action, reward, done, next_ob, neighbor_ob, neighbor_mask, neighbor_type, neighbor_relation, \
                    neighbor_distance, next_neighbor_ob, next_neighbor_mask, next_neighbor_type, next_neighbor_relation, next_neighbor_distance])

    def sample(self, batchsize, transpose=False):
        s, a, r, sp, d, ac, ns, nm, nt, nr, nd, nsp, nmp, ntp, nrp, ndp = \
            zip(*random.sample(self.replay, min(len(self.replay), batchsize)))
        if transpose:
            s, a, r, sp, ac, ns, nm, nt, nr, nd, nsp, nmp, ntp, nrp, ndp = \
                (list(zip(*i)) for i in [s, a, r, sp, ac, ns, nm, nt, nr, nd, nsp, nmp, ntp, nrp, ndp])
        return s, a, r, sp, d, ac, ns, nm, nt, nr, nd, nsp, nmp, ntp, nrp, ndp

        
def lerp(a, b, t):
    t = min(1, t)
    return a*(1-t)+b*t

def main():
    parser = get_config()
    parser.add_argument('--data', type=str, default='data/hangzhou')
    parser.add_argument('--step_count', type=int, default=3600)
    parser.add_argument('--interval', type=int, default=1)
    parser.add_argument('--reward', type=str, default='time')   
    parser.add_argument('--reward_weight', type=float, default=1)
    parser.add_argument('--tl_interval', type=int, default=15, help='interval of tl policies')
    parser.add_argument('--algo', choices=['ft_builtin', 'mp_builtin'], default='mp_builtin')
    parser.add_argument('--training_step', type=int, default=10000000)
    parser.add_argument('--training_start', type=int, default=2000)
    parser.add_argument('--junction_training_start', type=int, default=2000)
    parser.add_argument('--gamma', type=float, default=0.995)
    parser.add_argument('--batchsize', type=int, default=1024)
    parser.add_argument('--junction_batchsize', type=int, default=1024)
    parser.add_argument('--buffer_size', type=int, default=2**20)
    parser.add_argument('--lr', type=float, default=1e-5)
    parser.add_argument('--start_lr', type=float, default=5e-3)
    parser.add_argument('--cuda_id', type=int, default=0)
    parser.add_argument('--mlp', type=str, default='256,256')
    parser.add_argument("--load", type=int, default=0,help='pretrain (default: 0)')
    parser.add_argument("--record", type=int, default=0, help='whether to record the trajectories of vehicles')
    parser.add_argument('--yellow', type=int, default=0, help='yellow time duration')
    parser.add_argument('--save', type=int, default=0, help='save the model')
    parser.add_argument("--layer_N", type=int, default=1, help="Number of layers for actor/critic networks")   
    parser.add_argument("--experience_threshold", type=int, default=300, help="Number of experiences before training")
    parser.add_argument("--junction_experience_threshold", type=int, default=300, help="Number of junction experiences before training")
    parser.add_argument("--update_threshold", type=int, default=10, help="Number of experiences before updating")
    parser.add_argument("--target_vehicle_id", type=int, default=13, help='target vehicle id')
    parser.add_argument("--balancing_coef", type=float, default=0.5, help='balancing coefficient for two rewards')
    parser.add_argument("--first", type=int, default=0)
    parser.add_argument("--dqn_type", type=str, choices=['dqn', 'dueling'], default='dqn')
    parser.add_argument('--agg_type', type=str, choices=['none', 'bgcn', 'agg', 'corr_agg'], default='none')
    parser.add_argument('--lc_interval', type=int, default=300, help='interval of lc policies')
    parser.add_argument('--basic_update_times', type=int, default=1)
    parser.add_argument('--exploration_times', type=int, default=4000000)
    parser.add_argument("--alpha", type=float, default=0.2,help='balance of neighbour rewards')
    parser.add_argument("--junction_reward", type=str, choices=['queue', 'one_hop_queue'], default='one_hop_queue')
    parser.add_argument("--attn", type=str, choices=['type_sigmoid_attn', 'none'], default='type_sigmoid_attn')
    parser.add_argument('--distance', type=int, default=2, help='whether to use distance as edge feature')
    parser.add_argument('--junction_agg', type=int, default=1, help='whether to use neighbor junction information')
    parser.add_argument('--mean_field', type=int, default=1, help='whether to use the mean field information')
    parser.add_argument('--intention', type=int, default=1, help='whether to use the vehicle intention information in junction observation')
    parser.add_argument('--supervised_signal', type=int, default=0, help='whether to use supervised signal to guide adaptive graph construction')
    parser.add_argument('--emission_coef', type=float, default=1, help='emission coefficient')
    args = parser.parse_args()

    args.city = args.data.split('/')[-1]
    
    setproctitle.setproctitle('Router')

    path = 'log/router/{}_{}_gamma={}_start_lr={}_lr={}_batch_size={}_junction_batch_size={}_reward={}_rw={}_ts={}_et={}_ut={}_et={}_agg_type={}_jts={}_jet={}_mf={}_int={}_ss={}_ec={}_{}'.format(args.data, args.dqn_type, args.gamma, args.start_lr, args.lr, args.batchsize, args.junction_batchsize, args.reward, args.reward_weight, args.training_start, args.experience_threshold, args.update_threshold, args.exploration_times, args.agg_type, args.junction_training_start, args.junction_experience_threshold, args.mean_field, args.intention, args.supervised_signal, args.emission_coef, time.strftime('%d%H%M'))
    os.makedirs(path, exist_ok=True)
    with open(f'{path}/cmd.sh', 'w') as f:
        f.write(' '.join(sys.argv))
        f.write('\ntensorboard --port 8888 --logdir '+os.path.abspath(path))
    with open(f'{path}/args.json', 'w') as f:
        json.dump(vars(args), f)
    device = torch.device("cuda")
    os.environ['CUDA_VISIBLE_DEVICES'] = str(args.cuda_id)

    writer = SummaryWriter(path)
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s" % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    if args.algo == 'ft_builtin':
        base_algo = TlPolicy.FIXED_TIME
    elif args.algo == 'mp_builtin':
        base_algo = TlPolicy.MAX_PRESSURE
    else:
        raise NotImplementedError
    
    if args.agg_type == 'bgcn' or args.agg_type == 'none':
        args.agg = 1
        args.corr_agg = 1
    elif args.agg_type == 'agg':
        args.agg = 1
        args.corr_agg = 0
    elif args.agg_type == 'corr_agg':
        args.agg = 1
        args.corr_agg = 1
    else:
        raise NotImplementedError
    
    env = Env(
        data_path=args.data,
        step_size=args.interval,
        step_count=args.step_count//args.interval,
        log_dir='',
        base_algo=base_algo,
        reward=args.reward,
        tl_interval=args.tl_interval,
        yellow_time=args.yellow,
        save=args.save, 
        record=args.record, 
        args=args,
        reward_weight=args.reward_weight
    )

    args.road2adjroad = env.road2adjroad
    args.vehid2idxs = env.veh_id2idxs
    args.veh_dest_roads = env.veh_dest_roads
    args.persons = env.eng.get_persons(False)
    args.max_action_size = env.max_action_size
    args.agent_ids = env.selected_person_ids
    args.num_roads = env.num_roads

    ### 共享的replay
    replay = ReplayBuffer(args.buffer_size)
    j_replay = J_ReplayBuffer(args.buffer_size, env.num_junctions, env.junction_available_actions, phase_relation=env.junction_phase_relation_rearraged, neighbor_type=env.junction_neighbor_type_rearraged, \
        neighbor_idxs=env.junction_edge_list_rearraged, neighbor_distances=env.junction_edge_distance_rearraged, neighbor_masks=env.junction_neighbor_mask_rearraged)
    
    junction_neighbor_idxs = env.junction_edge_list_rearraged
    junction_neighbor_masks = env.junction_neighbor_mask_rearraged
    junction_neighbor_type = env.junction_neighbor_type_rearraged
    junction_neighbor_distances = env.junction_edge_distance_rearraged
    junction_phase_relation = env.junction_phase_relation_rearraged
    
    junction_neighbor_masks = torch.tensor(np.array(junction_neighbor_masks), dtype=torch.float32, device=device)
    junction_neighbor_type = torch.tensor(np.array(junction_neighbor_type), dtype=torch.float32, device=device)
    junction_neighbor_distances = torch.tensor(np.array(junction_neighbor_distances), dtype=torch.float32, device=device)
    junction_phase_relation = torch.tensor(np.array(junction_phase_relation), dtype=torch.float32, device=device)

    def cal_neighbor_obs(juncion_states):
        juncion_states = np.array(juncion_states)
        junction_neighbor_obs = np.zeros((juncion_states.shape[0], env.max_junction_neighbor_num, juncion_states.shape[1]))
        if junction_neighbor_idxs is not None:
            for i, idxs in enumerate(junction_neighbor_idxs):
                if idxs == []:
                    continue
                junction_neighbor_obs[i] = np.array(juncion_states[idxs, :]).copy()
        return junction_neighbor_obs
    
    vehs, roads = [], []   ## 初始时没有需要决策的vehs
    success_vehs = []
    episode_reward = 0
    time_reward, distance_reward, emission_reward = 0, 0, 0
    junction_reward = 0
    episode_count, episode_step, episode_num = 0, 0, 0
    best_episode_reward = -1e999
    best_junction_reward = -1e999
    args.steps = 0
    average_travel_time = 0
    average_finish_rate = 0
    actions = []

    basic_batch_size = args.batchsize
    basic_update_times = args.basic_update_times        
    junction_basic_batch_size = args.junction_batchsize
    junction_basic_update_times = args.basic_update_times
    replay_max = args.buffer_size

    added_experiences, added_junction_experiences = 0, 0
    training_count, j_training_count = 0, 0

    ## junction初始设定
    junction_states = env.junction_observe()
    junction_available_actions = env.junction_available_actions
    junction_actions = [0 for _ in range(env.num_junctions)]
    one_hot_action_matrix = env.one_hot_mapping_matrix[np.array(junction_actions).reshape(-1), :]
    junction_states = np.concatenate([junction_states, one_hot_action_matrix], axis=1)
    
    ### 存入replay
    j_replay.add_tmp({'junction_states': junction_states, 'junction_actions': None, 'junction_available_actions': junction_available_actions, 'junction_rewards': None, 'junction_dones': None})
    junction_obs_dim = junction_states.shape[1]
    if args.dqn_type == 'dqn':
        Q = BGCN_Actor(args, 
            source_obs_dim=env.source_state_dim*env.max_action_size+1, 
            obs_dim=env.neighbor_state_dim, 
            edge_dim=env.edge_dim,
            max_action=env.max_action_size, 
            roadidx2neighboridxs=env.roadidx2adjroadidx, 
            device=device).to(device)
        P = J_Actor(args, 
                    obs_dim=junction_obs_dim,
                    action_dim=env.max_junction_action_size, 
                    device=device).to(device) 
    elif args.dqn_type == 'dueling':
        Q = BGCN_Actor(args, 
            source_obs_dim=env.source_state_dim*env.max_action_size+1, 
            obs_dim=env.neighbor_state_dim, 
            edge_dim=env.edge_dim,
            max_action=env.max_action_size, 
            roadidx2neighboridxs=env.roadidx2adjroadidx, 
            device=device).to(device)
        P = VJ_Actor(args,
                    obs_dim=junction_obs_dim,
                    action_dim=env.max_junction_action_size, 
                    device=device).to(device)
    else:
        raise NotImplementedError

    warmup_steps = 10000
    total_steps = 2*10**6
    final_lr = args.lr
    initial_lr = args.start_lr
    def lr_schedule_fn(step):
        if step < warmup_steps:
            return step / warmup_steps
        else:
            progress = (step - warmup_steps) / (total_steps - warmup_steps)
            return max(1.0 - progress, final_lr / initial_lr)

    opt = torch.optim.Adam(Q.parameters(), lr=initial_lr)
    scheduler = LambdaLR(opt, lr_lambda=lr_schedule_fn)

    opt_j = torch.optim.Adam(P.parameters(), lr=initial_lr)
    scheduler_j = LambdaLR(opt_j, lr_lambda=lr_schedule_fn)

    Q_target = deepcopy(Q)
    Q_target = Q_target.to(device)

    P_target = deepcopy(P)
    P_target = P_target.to(device)

    all_edge_attrs = env.edge_attr_matrix

    with tqdm(range(args.training_step), ncols=100, smoothing=0.1) as bar:
        for step in bar:
            t0 = time.time()
            _st = time.time()
            eps = lerp(1, 0.05, step/args.exploration_times)   ### 到2000000步时epsilon为0.05      ### 0408 修改为1000000尝试一下
            #### 路径选择决策
            if len(vehs) > 0:
                # 需要读取obs和available_actions
                obs = torch.tensor(np.array(obs), dtype=torch.float32, device=device)
                available_actions = np.array(available_actions)
                if args.agg == 1:
                    obs_all = torch.tensor(np.array(obs_all), dtype=torch.float32, device=device)
                    edge_attrs = all_edge_attrs[dest_idxs, :, :, :]
                    edge_attrs = torch.tensor(np.array(edge_attrs), dtype=torch.float32, device=device)
                ac = available_actions.sum(axis=1)
                corr_adj_matrice = torch.tensor(np.array(corr_adj_matrice), dtype=torch.float32, device=device)

                action_explore = [random.randint(0, a-1) for a in ac]
                if step < args.training_start:
                    actions = action_explore
                else:
                    with torch.no_grad():
                        if args.supervised_signal == 0:
                            m = Q(obs, obs_all, edge_attrs, ridxs, corr_adj_matrice)
                        else:
                            m, _, _ = Q(obs, obs_all, edge_attrs, ridxs, corr_adj_matrice)
                        m[available_actions==0] = -1e9
                        action_exploit = torch.argmax(m, dim=-1).cpu().numpy()
                    actions = np.choose(np.random.uniform(size=len(ac)) < eps, [action_explore, action_exploit]).tolist()

                ### 释放显存
                del obs, obs_all, edge_attrs, corr_adj_matrice  

            #### 信控选择决策
            if args.steps % args.tl_interval == 0:
                junction_neighbor_obs = cal_neighbor_obs(junction_states)
                junction_neighbor_states = torch.tensor(np.array(junction_neighbor_obs), dtype=torch.float32, device=device)
                ### random explore
                junction_action_explore = [junction_available_actions[i].nonzero()[0][random.randint(0, len(junction_available_actions[i].nonzero()[0])-1)] for i in range(len(junction_available_actions))]
                if step < args.training_start:
                    junction_actions = junction_action_explore
                else:
                    with torch.no_grad():
                        p = P(junction_states, junction_neighbor_states, junction_neighbor_masks, junction_neighbor_type, junction_phase_relation, junction_neighbor_distances)
                        p[torch.tensor(np.array(junction_available_actions), dtype=torch.float32, device=device)==0] = -1e9
                        junction_action_exploit = torch.argmax(p, dim=-1).cpu().numpy()
                    junction_actions = np.choose(np.random.uniform(size=len(junction_available_actions)) < eps, [junction_action_explore, junction_action_exploit]).tolist()
            
                    ### 释放显存
                    del junction_states, junction_neighbor_states

            new_experiences, next_vehs, success_vehs, infos, next_states, next_all_states, next_dest_idxs, next_ridxs, next_acs, next_corr_adj_matrice, next_junction_states, junction_experiences = env.step(actions, junction_actions)
            added_experiences += replay.add_tmp(new_experiences)
            j_replay.add_tmp(junction_experiences)
             
            episode_reward += infos['rewards']
            time_reward += infos['time_reward']
            distance_reward += infos['distance_reward']
            emission_reward += infos['emission_reward']

            if infos['junction_reward'] is not None:
                junction_reward += np.mean(infos['junction_reward'])

            episode_count += 1
            episode_step += 1
            if step >= args.training_start and added_experiences >  args.experience_threshold:
                ### 更新路径导航
                replay_len = replay.len()
                k = 1 + replay_len / replay_max

                batch_size   = int(k * basic_batch_size)
                update_times = int(k * basic_update_times)
                overall_loss = 0
                rl_loss, supervised_loss = 0, 0
                for _ in range(update_times):
                    s, a, ac, r, d, sa, dixs, ridxs, cam, next_s, next_sa, next_dixs, next_cam = \
                        replay.sample(batch_size, transpose=False)
                    d = torch.tensor(d, dtype=torch.float32, device=device)
                    loss = 0
                    s = torch.tensor(np.array(s), dtype=torch.float32, device=device)
                    a = torch.tensor(a, dtype=torch.long, device=device)
                    r = torch.tensor(r, dtype=torch.float32, device=device)
                    next_s = torch.tensor(np.array(next_s), dtype=torch.float32, device=device)
                    ac = torch.tensor(ac, dtype=torch.float32, device=device)
                    cam = torch.tensor(np.array(cam), dtype=torch.float32, device=device)
                    next_cam = torch.tensor(np.array(next_cam), dtype=torch.float32, device=device)
                    if args.agg == 1:
                        sa = torch.tensor(np.array(sa), dtype=torch.float32, device=device)
                        ea = all_edge_attrs[dixs, :, :, :]
                        ea = torch.tensor(np.array(ea), dtype=torch.float32, device=device)
                        next_sa = torch.tensor(np.array(next_sa), dtype=torch.float32, device=device)
                        next_ea = all_edge_attrs[next_dixs, :, :, :]
                        next_ea = torch.tensor(np.array(next_ea), dtype=torch.float32, device=device)
                    ridxs = list(ridxs)

                    with torch.no_grad():
                        if args.supervised_signal == 0:
                            m = Q_target(next_s, next_sa, next_ea, ridxs, next_cam)
                        else:
                            m, _, _ = Q_target(next_s, next_sa, next_ea, ridxs, next_cam)
                        # m[ac==0] = -1e9
                        y_target = r+args.gamma*m.max(1).values*(1-d)
                    if args.supervised_signal == 0:    
                        m = Q(s, sa, ea, ridxs, cam)
                        y = m.gather(-1, a[..., None]).view(-1)
                    else:
                        m, obs_predict, obs_true = Q(s, sa, ea, ridxs, cam)
                        y = Q(s, sa, ea, ridxs, cam)[0].gather(-1, a[..., None]).view(-1)
                    loss = loss+F.mse_loss(y, y_target)
                    rl_loss += F.mse_loss(y, y_target).item()
                    if args.supervised_signal == 1:
                        loss = loss + F.mse_loss(obs_predict, obs_true)
                        supervised_loss += F.mse_loss(obs_predict, obs_true).item()
                    opt.zero_grad()
                    loss.backward()
                    opt.step()
                    scheduler.step()
                    overall_loss += loss.item()
                overall_loss /= update_times
                rl_loss /= update_times
                supervised_loss /= update_times
                writer.add_scalar('metric/overall_loss', overall_loss, step)
                writer.add_scalar('metric/rl_loss', rl_loss, step)
                writer.add_scalar('metric/supervised_loss', supervised_loss, step)
                added_experiences = 0
                training_count += 1
                if training_count % args.update_threshold == 0:
                    Q_target.load_state_dict(Q.state_dict())
                    training_count = 0
                del s, a, ac, r, d, sa, dixs, ridxs, cam, next_s, next_sa, next_dixs, next_cam

            if step > args.junction_training_start and added_junction_experiences > args.junction_experience_threshold:
                ### 更新信控
                replay_len = replay.len()
                k = 1 + replay_len / replay_max

                junction_batch_size   = int(k * junction_basic_batch_size)
                update_times = int(k * basic_update_times)
                overall_loss = 0
                for _ in range(update_times):
                    s, a, r, sp, d, ac, ns, nm, nt, nr, nd, nsp, nmp, ntp, nrp, ndp = j_replay.sample(junction_batch_size, transpose=False)
                    d = torch.tensor(d, dtype=torch.float32, device=device)
                    loss = 0
                    s = torch.tensor(np.array(s), dtype=torch.float32, device=device)
                    a = torch.tensor(a, dtype=torch.long, device=device)
                    r = torch.tensor(r, dtype=torch.float32, device=device)
                    sp = torch.tensor(np.array(sp), dtype=torch.float32, device=device)
                    ac = torch.tensor(ac, dtype=torch.float32, device=device)
                    ns = torch.tensor(np.array(ns), dtype=torch.float32, device=device)
                    nm = torch.tensor(np.array(nm), dtype=torch.float32, device=device)
                    nt = torch.tensor(np.array(nt), dtype=torch.float32, device=device)
                    nr = torch.tensor(np.array(nr), dtype=torch.float32, device=device)
                    nd = torch.tensor(np.array(nd), dtype=torch.float32, device=device)
                    nsp = torch.tensor(np.array(nsp), dtype=torch.float32, device=device)
                    nmp = torch.tensor(np.array(nmp), dtype=torch.float32, device=device)
                    ntp = torch.tensor(np.array(ntp), dtype=torch.float32, device=device)
                    nrp = torch.tensor(np.array(nrp), dtype=torch.float32, device=device)
                    ndp = torch.tensor(np.array(ndp), dtype=torch.float32, device=device)
                    with torch.no_grad():
                        m = P_target(sp, nsp, nmp, ntp, nrp, ndp)
                        # m[ac==0] = -1e9
                        y_target = r+args.gamma*m.max(1).values*(1-d)
                    y = P(s, ns, nm, nt, nr, nd).gather(-1, a[..., None]).view(-1)
                    loss = loss+F.mse_loss(y, y_target)
                    opt_j.zero_grad()
                    loss.backward()
                    opt_j.step()
                    scheduler_j.step()
                    overall_loss += loss.item()
                overall_loss /= update_times
                writer.add_scalar('metric/overall_loss_j', overall_loss, step)
                added_junction_experiences = 0
                j_training_count += 1
                if j_training_count % args.update_threshold == 0:
                    P_target.load_state_dict(P.state_dict())
                    j_training_count = 0
                del s, a, r, sp, d, ac, ns, nm, nt, nr, nd, nsp, nmp, ntp, nrp, ndp

            vehs = next_vehs
            obs = next_states
            obs_all = next_all_states
            dest_idxs = next_dest_idxs
            ridxs = next_ridxs
            available_actions = next_acs
            corr_adj_matrice = next_corr_adj_matrice

            junction_states = next_junction_states

            args.steps += 1

            if args.steps//args.interval % args.step_count == 0:
                if args.first == 1:
                    print(replay.len())
                    exit()
                writer.add_scalar('metric/EpisodeReward', episode_reward, episode_num)
                writer.add_scalar('metric/time_reward', time_reward, episode_num)
                writer.add_scalar('metric/distance_reward', distance_reward, episode_num)
                writer.add_scalar('metric/emission_reward', emission_reward, episode_num)
                writer.add_scalar('metric/junction_reward', junction_reward, episode_num)
                writer.add_scalar('metric/overall_reward', episode_reward+junction_reward, episode_num)
                if episode_reward > best_episode_reward:
                    ### 记录actions序列
                    with open(f'{path}/vehicle_actions.json', 'w') as f:
                        env.vehicle_actions_record = {str(k): v for k, v in env.vehicle_actions_record.items()}
                        json.dump(env.vehicle_actions_record, f)
                    with open(f'{path}/junction_actions.json', 'w') as f:
                        json.dump(str(env.junction_actions_record), f)
                    best_episode_reward = episode_reward
                    writer.add_scalar('metric/Best_EpisodeReward', episode_reward)      
                    writer.add_scalar('metric/Best_ATT', infos['ATT'])
                    writer.add_scalar('metric/Best_ATT_finished', infos['ATT_success'])
                    writer.add_scalar('metric/Best_Throughput', infos['Throughput'])
                    writer.add_scalar('metric/Best_VEH', infos['VEH'])
                    writer.add_scalar('metric/Best_Emissions', infos['emissions'])
                    save(Q, P, path, 'best')
                if junction_reward > best_junction_reward:
                    best_junction_reward = junction_reward
                    writer.add_scalar('metric/Best_JunctionReward', junction_reward)
                    writer.add_scalar('metric/Best_Junction_ATT', infos['ATT'])
                    writer.add_scalar('metric/Best_Junction_ATT_finished', infos['ATT_success'])
                    writer.add_scalar('metric/Best_Junction_Throughput', infos['Throughput'])
                    writer.add_scalar('metric/Best_Junction_VEH', infos['VEH'])
                    save(Q, P, path, 'best_j')
                episode_num += 1
                episode_reward = 0
                time_reward, distance_reward, emission_reward = 0, 0, 0
                junction_reward = 0
                episode_count = 0
                writer.add_scalar('metric/ATT', infos['ATT'], step)
                writer.add_scalar('metric/ATT_finished', infos['ATT_success'], step)
                writer.add_scalar('metric/Throughput', infos['Throughput'], step)
                writer.add_scalar('metric/Emissions', infos['emissions'], step)
                writer.add_scalar('metric/VEH', infos['VEH'], step)
                args.steps = 0
                vehs = []
                obs = []
                available_actions = []
                bar.set_description(f'Step: {step}, ATT: {infos["ATT"]:.2f}, TP: {infos["Throughput"]}')

                all_rewards = infos['rewards']
                writer.add_scalar('metric/Reward', all_rewards, step)            
                writer.add_scalar('chart/FPS', 1/(time.time()-_st), step)
                episode_step = 0

                env.junction_actions_record = []
                env.vehicle_actions_record = {}

            actions = []
            junction_actions = []   


if __name__ == '__main__':
    main()
