import time
from selenium.common.exceptions import TimeoutException

class WebDriverWait:
    def __init__(self, driver, timeout, poll_frequency=0.1):
        self.driver = driver; self.timeout = timeout; self.poll = poll_frequency
    def until(self, condition, message=""):
        end = time.monotonic() + float(self.timeout)
        last = None
        while True:
            try:
                value = condition(self.driver)
                if value:
                    return value
            except Exception as exc:
                last = exc
            if time.monotonic() > end:
                raise TimeoutException(message or f"attente expirée ({last})")
            time.sleep(self.poll)
