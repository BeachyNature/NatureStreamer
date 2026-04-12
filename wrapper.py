import time

def pprint(msg:str) -> None:
    """ Pretty print with timestamp """

    print(f"[{time.strftime('%H:%M:%S')}] {msg}")