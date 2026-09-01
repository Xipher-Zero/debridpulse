"""Local capacity admission; existing executions may finish while dispatch waits."""
import shutil


class DiskCapacity:
    def __init__(self, root, minimum_gb=0, hysteresis_gb=0.5):
        self.root = root
        self.minimum_gb = max(0, minimum_gb)
        self.hysteresis_gb = max(0, hysteresis_gb)
        self.active = False

    def check(self):
        result = {"enabled": self.minimum_gb > 0, "active": self.active, "min_free_gb": self.minimum_gb, "free_gb": -1.0}
        if self.minimum_gb <= 0:
            self.active = False
        else:
            try:
                free = shutil.disk_usage(self.root).free / (1024 ** 3)
                result["free_gb"] = free
                if free <= self.minimum_gb:
                    self.active = True
                elif free >= self.minimum_gb + self.hysteresis_gb:
                    self.active = False
            except OSError:
                result["error"] = "Free disk space is unavailable"
        result["active"] = self.active
        return result
