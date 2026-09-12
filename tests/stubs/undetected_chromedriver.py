"""Stub minimal de undetected-chromedriver pour tester la logique hors ligne."""
class ChromeOptions:
    def __init__(self): self.args = []; self.prefs = {}
    def add_argument(self, a): self.args.append(a)
    def add_experimental_option(self, k, v): self.prefs[k] = v
class Chrome:
    def __init__(self, *a, **k): raise RuntimeError("stub: pas de vrai Chrome")
