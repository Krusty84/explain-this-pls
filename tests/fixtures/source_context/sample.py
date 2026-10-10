import os
from pathlib import Path
class Box:
    async def run(self):
        def inner():
            return 1
        return inner()
def top():
    return Box()
