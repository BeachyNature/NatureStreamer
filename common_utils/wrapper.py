import os
import yaml
import time

def pprint(msg:str) -> None:
    """ Pretty print with timestamp """

    print(f"[{time.strftime('%H:%M:%S')}] {msg}")

def read_yaml(path:str) -> dict:
    """ Read a yaml file """

    if not os.path.exists(path):
        pprint("Unable to read yaml file...")
        return {}
    
    with open(path, 'r') as f:
        data = yaml.safe_load(f)
    return data
