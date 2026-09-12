from selenium.common.exceptions import NoSuchElementException

class presence_of_element_located:
    def __init__(self, locator): self.locator = locator
    def __call__(self, driver):
        try:
            el = driver.find_element(self.locator[0], self.locator[1])
            return el if el else False
        except NoSuchElementException:
            return False

class visibility_of_element_located(presence_of_element_located):
    pass
