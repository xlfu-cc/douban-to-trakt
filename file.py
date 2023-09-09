import sys
import os

class WorkingDir:
    @classmethod
    def get(cls, name, ensure_parent = False):
        work_dir = ""
        if getattr(sys, "frozen", False):
            work_dir = os.path.dirname(sys.executable)
        elif __file__:
            work_dir = os.path.dirname(__file__)

        path = os.path.join(work_dir, name)
        if ensure_parent:
            directory = os.path.dirname(path)
            if not os.path.exists(directory):
                os.makedirs(directory, exist_ok=True)
        return path
    
    @classmethod
    def get_output(cls, name, ensure_parent = True):
        return cls.get(f"output/{name}", ensure_parent)
    