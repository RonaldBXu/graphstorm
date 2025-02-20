"""
    Copyright 2023 Contributors

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Training entry point.
"""

import json
import logging
import os
import queue
import socket
import subprocess
import sys
import time
from threading import Thread, Event

import boto3
import sagemaker
from ..config import (BUILTIN_TASK_NODE_CLASSIFICATION,
                      BUILTIN_TASK_NODE_REGRESSION,
                      BUILTIN_TASK_EDGE_CLASSIFICATION,
                      BUILTIN_TASK_EDGE_REGRESSION,
                      BUILTIN_TASK_LINK_PREDICTION,
                      BUILTIN_TASK_MULTI_TASK)
from .utils import (download_yaml_config,
                    download_graph,
                    get_job_port,
                    keep_alive,
                    barrier_master,
                    barrier,
                    terminate_workers,
                    wait_for_exit,
                    download_model,
                    upload_model_artifacts)

def launch_train_task(task_type, num_gpus, graph_config,
    save_model_path, ip_list, yaml_path,
    extra_args, state_q, custom_script, restore_model_path=None):
    """ Launch SageMaker training task

    Parameters
    ----------
    task_type: str
        Task type. It can be node classification/regression,
        edge classification/regression, link prediction, etc.
        Refer to graphstorm.config.config.SUPPORTED_TASKS for more details.
    num_gpus: int
        Number of gpus per instance
    graph_config: str
        Where does the graph partition config reside.
    save_model_path: str
        Output path to save models
    ip_list: str
        Where does the ip list reside.
    yaml_path: str
        Where does the yaml config file reside.
    extra_args: list
        Training args
    state_q: queue.Queue()
        A queue used to return execution result (success or failure)
    custom_script: str
        Custom training script provided by a customer to run customer training logic.
    restore_model_path: str
        Path for the model to restore for model fine-tuning.
        Default: None

    Return
    ------
    Thread: training task thread
    """
    if custom_script is not None:
        cmd = "graphstorm.run.launch"
    elif task_type == BUILTIN_TASK_NODE_CLASSIFICATION:
        cmd = "graphstorm.run.gs_node_classification"
    elif task_type == BUILTIN_TASK_NODE_REGRESSION:
        cmd = "graphstorm.run.gs_node_regression"
    elif task_type == BUILTIN_TASK_EDGE_CLASSIFICATION:
        cmd = "graphstorm.run.gs_edge_classification"
    elif task_type == BUILTIN_TASK_EDGE_REGRESSION:
        cmd = "graphstorm.run.gs_edge_regression"
    elif task_type == BUILTIN_TASK_LINK_PREDICTION:
        cmd = "graphstorm.run.gs_link_prediction"
    elif task_type == BUILTIN_TASK_MULTI_TASK:
        cmd = "graphstorm.run.gs_multi_task_learning"
    else:
        raise RuntimeError(f"Unsupported task type {task_type}")

    launch_cmd = ["python3", "-u", "-m", cmd,
        "--num-trainers", f"{num_gpus if int(num_gpus) > 0 else 1}",
        "--num-servers", "1",
        "--num-samplers", "0",
        "--part-config", f"{graph_config}",
        "--ip-config", f"{ip_list}",
        "--extra-envs", f"LD_LIBRARY_PATH={os.environ['LD_LIBRARY_PATH']} ",
        "--ssh-port", "22",
        "--do-nid-remap", "False" # No need to do nid map in SageMaker trianing.
        ]
    launch_cmd += [custom_script] if custom_script is not None else []
    launch_cmd += ["--cf", f"{yaml_path}",
        "--save-model-path", f"{save_model_path}"]
    launch_cmd += ["--restore-model-path", f"{restore_model_path}"] \
            if restore_model_path is not None else []
    launch_cmd += extra_args
    logging.debug("Launch training %s", launch_cmd)

    def run(launch_cmd, state_q):
        try:
            subprocess.check_call(launch_cmd, shell=False)
            state_q.put(0)
        except subprocess.CalledProcessError as err:
            logging.error("Called process error %s", err)
            state_q.put(err.returncode)
        except Exception: # pylint: disable=broad-except
            state_q.put(-1)

    thread = Thread(target=run, args=(launch_cmd, state_q,), daemon=True)
    thread.start()
    # sleep for a while in case of ssh is rejected by peer due to busy connection
    time.sleep(0.2)
    return thread

def run_train(args, unknownargs):
    """ Main logic

        args should provide following arguments
        task_type: str
            Training task type.
        graph_name: str
            The name of the graph.
        graph_data_s3: str
            S3 location of input training graph.
        train_yaml_s3: str
            S3 location of training yaml file.
        model_artifact_s3: str
            S3 location to store the model artifacts.
        custom_script: str
            Custom training script provided by a customer to run
            customer training logic. Can be None.
        data_path: str
            Local working path.
        num_gpus: int
            Number of gpus.
        sm_dist_env: json str
            SageMaker distributed env.
        region: str
            AWS Region.
    """
    num_gpus = args.num_gpus
    data_path = args.data_path
    model_checkpoint_s3 = args.model_checkpoint_to_load
    if model_checkpoint_s3 is not None:
        restore_model_path = "/tmp/gsgnn_model_checkpoint/"
        os.makedirs(restore_model_path, exist_ok=True)
    else:
        restore_model_path = None

    if args.model_artifact_s3:
        # If a user provides an S3 output destination as an input arg, the script itself
        # will upload the model artifacts after training, so we save under /tmp.
        output_path = "/tmp/gsgnn_model/"
    else:
        # If the user does not provide an output destination as an arg, we rely on SageMaker to
        # do the model upload so we save the model to the pre-determined path /opt/ml/model
        output_path = "/opt/ml/model"

    os.makedirs(output_path, exist_ok=True)

    # start the ssh server
    subprocess.run(["service", "ssh", "start"], check=True)

    logging.info("Known args %s", args)
    logging.info("Unknown args %s", unknownargs)

    save_model_path = os.path.join(output_path, "model_checkpoint")

    train_env = json.loads(args.sm_dist_env)
    hosts = train_env['hosts']
    current_host = train_env['current_host']
    world_size = len(hosts)
    os.environ['WORLD_SIZE'] = str(world_size)
    host_rank = hosts.index(current_host)

    # NOTE: Ensure no logging has been done before setting logging configuration
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), None),
        format=f'{current_host}: %(asctime)s - %(levelname)s - %(message)s',
        force=True)

    try:
        for host in hosts:
            logging.info("The %s IP is %s", host, socket.gethostbyname(host))
    except:
        raise RuntimeError(f"Can not get host name of {hosts}")

    master_addr = args.master_addr
    master_port = get_job_port(train_env['job_name'])
    # sync with all instances in the cluster
    if host_rank == 0:
        # sync with workers
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((master_addr, master_port))
        sock.listen(world_size)
        logging.info("Master listening on %s:%s", master_addr, master_port)

        client_list = [None] * world_size
        for i in range(1, world_size):
            client, _ = sock.accept()
            client_list[i] = client
    else:
        # sync with master
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for _ in range(30):
            try:
                sock.connect((master_addr, master_port))
                break
            except: # pylint: disable=bare-except
                logging.info("Trying to connect to %s:%s...", master_addr, master_port)
                time.sleep(10)
        logging.info("Connected to %s:%s", master_addr, master_port)

    # write ip list info into disk
    ip_list_path = os.path.join(data_path, 'ip_list.txt')
    with open(ip_list_path, 'w', encoding='utf-8') as f:
        for host in hosts:
            f.write(f"{socket.gethostbyname(host)}\n")

    gs_params = unknownargs
    graph_name = args.graph_name
    graph_data_s3 = args.graph_data_s3
    task_type = args.task_type
    train_yaml_s3 = args.train_yaml_s3
    # If the user provided an output destination, trim any trailing '/'
    if args.model_artifact_s3:
        gs_model_artifact_s3 = args.model_artifact_s3.rstrip('/')
    else:
        gs_model_artifact_s3 = None
    custom_script = args.custom_script

    boto_session = boto3.session.Session(region_name=args.region)
    sagemaker_session = sagemaker.session.Session(boto_session=boto_session)
    yaml_path = download_yaml_config(train_yaml_s3,
        data_path, sagemaker_session)
    graph_config_path = download_graph(graph_data_s3, graph_name,
        host_rank, world_size, data_path, sagemaker_session)
    if model_checkpoint_s3 is not None:
        # Download Saved model checkpoint to resume
        download_model(model_checkpoint_s3, restore_model_path, sagemaker_session)
        logging.info("Successfully downloaded the model into %s.\n The model files are: %s.",
                     restore_model_path, os.listdir(restore_model_path))

    err_code = 0
    if host_rank == 0:
        barrier_master(client_list, world_size)

        # launch a thread to send keep alive message to all workers
        task_end = Event()
        thread = Thread(target=keep_alive,
            args=(client_list, world_size, task_end),
            daemon=True)
        thread.start()

        try:
            # launch distributed training here
            state_q = queue.Queue()
            train_task = launch_train_task(task_type,
                                            num_gpus,
                                            graph_config_path,
                                            save_model_path,
                                            ip_list_path,
                                            yaml_path,
                                            gs_params,
                                            state_q,
                                            custom_script,
                                            restore_model_path)
            train_task.join()
            err_code = state_q.get()
        except RuntimeError as e:
            print(e)
            err_code = -1
        # Indicate we can stop sending keepalive messages
        task_end.set()
        # Ensure the keepalive thread has finished before closing sockets
        thread.join()
        terminate_workers(client_list, world_size)
        logging.info("Master End")
    else:
        barrier(sock)
        # Block util training finished
        # Listen to end command
        wait_for_exit(sock)
        logging.info("Worker End")

    sock.close()
    if err_code != 0:
        # Report an error
        logging.error("Task failed")
        sys.exit(-1)

    # We upload models only when the user explicitly set the model_artifact_s3
    # argument. Otherwise we can rely on the SageMaker service to do the upload.
    if gs_model_artifact_s3 and os.path.exists(save_model_path):
        upload_model_artifacts(gs_model_artifact_s3, save_model_path, sagemaker_session)
