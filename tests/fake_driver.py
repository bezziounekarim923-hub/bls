"""Pilote Selenium factice pour tester bls_espagne_prefill.py hors ligne."""

import re

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)

PAGE_SOURCE_LONG = "<html><body>" + ("contenu BLS " * 80) + "</body></html>"
LOGIN_URL = "https://algeria.blsinternational.com/"
APPOINTMENT_URL = "https://algeria.blsinternational.com/es/fr/appointment"

URLS = {
    "calendar": APPOINTMENT_URL,
    "login": "https://algeria.blsinternational.com/es/fr/login",
    "account": "https://algeria.blsinternational.com/es/fr/my-account",
    "blank": "https://algeria.blsinternational.com/es/fr/erreur",
    # Vue « liste des rendez-vous » d'une SPA : page chargée, mais sans calendrier
    "list": "https://algeria.blsinternational.com/manage-appointments",
}

MONTHS = ["septembre 2026", "octobre 2026", "novembre 2026"]


_ELEMENT_SEQ = [0]


class FakeElement:
    def __init__(self, tag="td", attrs=None, text="", on_click=None, enabled=True,
                 on_keys=None, driver=None, generation=None):
        _ELEMENT_SEQ[0] += 1
        self.id = f"el-{_ELEMENT_SEQ[0]}"
        self.on_keys = on_keys
        self.tag_name = tag
        self._attrs = attrs or {}
        self._text = text
        self.on_click = on_click
        self.clicks = 0
        self.js_clicks = 0
        self._enabled = enabled
        self.sent_keys = []
        # Simulation des re-rendus React/Radix : un élément créé avant le
        # dernier changement de DOM est « détaché » (stale), comme en vrai.
        self._driver = driver
        self._generation = generation

    def _check_stale(self):
        if (self._driver is not None and self._generation is not None
                and self._driver._generation != self._generation):
            raise StaleElementReferenceException(
                "élément détaché du DOM (re-rendu React/Radix)")

    @property
    def text(self):
        self._check_stale()
        return self._text

    @text.setter
    def text(self, value):
        self._text = value

    def get_attribute(self, name):
        self._check_stale()
        return self._attrs.get(name)

    def click(self):
        self._check_stale()
        self.clicks += 1
        if self.on_click is not None:
            self.on_click()

    def js_click(self):
        """Clic déclenché par execute_script (séquence d'événements pointer)."""
        self._check_stale()
        self.js_clicks += 1
        if self.on_click is not None:
            self.on_click()

    def clear(self):
        pass

    def send_keys(self, *values):
        self._check_stale()
        self.sent_keys.extend(values)
        if self.on_keys is not None:
            self.on_keys(values)

    def is_enabled(self):
        return self._enabled

    def is_displayed(self):
        return True


class FakeSwitchTo:
    def window(self, handle):
        return None


class FakeDriver:
    """
    Simule les pages du site :

    - "calendar" : calendrier rendu, mois courant = MONTHS[month_index]
    - "login"    : formulaire de connexion (session expirée)
    - "account"  : espace compte (connecté)
    - "blank"    : page sans calendrier
    """

    def __init__(self, page="calendar", months_available=None, logged_in=True, alive=True,
                 stale_menus=False):
        self.page = page
        # stale_menus=True : chaque ouverture/fermeture de menu re-rend la
        # liste (comme React/Radix en vrai) et détache les éléments précédents.
        self.stale_menus = stale_menus
        self._generation = 0
        self.months_available = dict(months_available or {})
        self.month_index = 0
        self.logged_in = logged_in
        self.alive = alive
        self._title = "BLS International"
        self._page_source = PAGE_SOURCE_LONG
        self._current_url = URLS[page]
        self.on_get = None
        self.links = []          # [{"text": "Book Appointment", "href": ..., "page": "calendar"}]
        # Menus déroulants Radix : {"trigger": "More actions",
        #   "items": ["Cancel Appointment", "Continue to slot selection"],
        #   "page": "calendar"}  -> l'entrée « slot » ouvre `page`
        self.dropdowns = []
        self.gets = []
        self.raise_timeout_on_get = 0
        self.switch_to = FakeSwitchTo()
        self.window_handles = ["w1"]
        self.current_window_handle = "w1"
        self._clicked_days = []

    # ---- Attributs qui lèvent quand la session est morte (comme Selenium) ----

    @property
    def current_url(self):
        if not self.alive:
            raise InvalidSessionIdException("session WebDriver morte")
        return self._current_url

    @current_url.setter
    def current_url(self, value):
        self._current_url = value

    @property
    def title(self):
        if not self.alive:
            raise InvalidSessionIdException("session WebDriver morte")
        return self._title

    @title.setter
    def title(self, value):
        self._title = value

    @property
    def page_source(self):
        if not self.alive:
            raise InvalidSessionIdException("session WebDriver morte")
        return self._page_source

    @page_source.setter
    def page_source(self, value):
        self._page_source = value

    # ---- API Selenium minimale ----

    def get(self, url):
        if not self.alive:
            raise InvalidSessionIdException("session WebDriver morte")
        self.gets.append(url)
        if self.raise_timeout_on_get > 0:
            self.raise_timeout_on_get -= 1
            raise TimeoutException("chargement trop lent")
        if self.on_get is not None:
            self.on_get(url, self)
        self.current_url = URLS.get(self.page, url)
        if self.page == "login":
            self.title = "Login - BLS"
            self.body_text = "Session expired. Please login again."
        elif self.page == "list":
            self.title = "Manage Appointments"
            self.body_text = "Vos rendez-vous. Book Appointment"
        elif self.page == "blank":
            self.title = "Erreur"
            self.body_text = "Une erreur est survenue."
        elif self.page == "account":
            self.title = "My Account"
            self.body_text = "Bonjour, votre compte."
        else:
            self.title = "Prendre rendez-vous"
            self.body_text = "No slots available for this month."

    body_text = ""

    def maximize_window(self):
        return None

    def set_window_position(self, x, y):
        return None

    def set_page_load_timeout(self, value):
        self.page_load_timeout = value

    def set_script_timeout(self, value):
        self.script_timeout = value

    def execute_script(self, script, *args):
        # Le clic de secours du script rejoue une séquence d'événements
        # (pointerdown/pointerup/click) : on la matérialise ici.
        if args and ("dispatchEvent" in script or "arguments[0].click()" in script):
            element = args[0]
            if hasattr(element, "js_click"):
                element.js_click()
            elif hasattr(element, "click"):
                element.click()
        return None

    def quit(self):
        self.alive = False

    def find_element(self, by, selector):
        elements = self.find_elements(by, selector)
        if not elements:
            raise NoSuchElementException(selector)
        return elements[0]

    def find_elements(self, by, selector):
        if not self.alive:
            raise InvalidSessionIdException("session WebDriver morte")
        sel = selector

        # --- formulaire de connexion ---
        if "input[type='password']" in sel or "input[name*='pass" in sel or "input[id*='pass" in sel:
            if self.page == "login":
                return [FakeElement("input", {"type": "password", "name": "password"})]
            return []
        if "input[type='email']" in sel or "email" in sel or "autocomplete='username'" in sel:
            if self.page == "login":
                return [FakeElement("input", {"type": "email", "name": "email"})]
            return []
        if "button[type='submit']" in sel:
            if self.page == "login":
                return [FakeElement("button", {"type": "submit"}, text="Login", on_click=self._submit_login)]
            return []

        # --- corps de page (garde-fou session, mode deep) ---
        if by == "tag name" and sel == "body":
            return [FakeElement("body", text=self.body_text, on_click=None,
                                on_keys=self._on_body_keys)]

        # --- calendrier ---
        if "rdp-availability_" in sel:
            return self._available_day_elements()
        if "rdp-caption" in sel:
            if self.page == "calendar":
                return [FakeElement("div", {"class": "rdp-caption_label"}, text=self.month_label())]
            return []
        if "rdp-nav" in sel or "aria-label*='month'" in sel:
            if self.page != "calendar":
                return []
            return [self._nav_button("previous"), self._nav_button("next")]
        if sel.startswith("td[class*='rdp-']"):
            if self.page == "calendar":
                return [FakeElement("td", {"class": "rdp-day"}) for _ in range(30)]
            return []

        # --- menus déroulants Radix ---
        if "dropdown-menu-trigger" in sel or "aria-haspopup" in sel:
            return [self._dropdown_trigger(menu) for menu in self.dropdowns]
        if "dropdown-menu-item" in sel or "menuitem" in sel:
            items = []
            for menu in self.dropdowns:
                if not menu.get("open"):
                    continue
                for text in menu["items"]:
                    items.append(self._menu_item(menu, text))
            return items

        # --- liens/boutons (recherche par texte XPath ou par href) ---
        if by == "xpath":
            phrases = [
                phrase for phrase in re.findall(r"'([^']+)'", sel)
                if len(phrase) <= 30 and phrase.lower() != "abcdefghijklmnopqrstuvwxyz"
            ]
            return [
                self._link_element(link) for link in self.links
                if any(ph.lower() in (link.get("text") or "").lower() for ph in phrases)
            ]
        if sel.startswith("a[href*="):
            keyword = sel.split("='", 1)[1].rstrip("']").lower()
            return [
                self._link_element(link) for link in self.links
                if keyword in (link.get("href") or "").lower()
            ]

        # --- CAPTCHA ---
        if "recaptcha" in sel or "captcha" in sel or "sitekey" in sel or "challenge" in sel:
            return []

        return []

    def _on_body_keys(self, keys):
        # Échap referme les menus ouverts (send_keys transmet un tuple)
        pressed = "".join(str(key) for key in (keys if isinstance(keys, (tuple, list)) else [keys]))
        if "\ue00c" in pressed:
            closed = False
            for menu in self.dropdowns:
                if menu.get("open"):
                    closed = True
                menu["open"] = False
            if closed:
                self._bump_generation()

    def _bump_generation(self):
        if self.stale_menus:
            self._generation += 1

    def _tracked(self, element):
        """Élément rattaché à la génération courante du DOM (si suivi activé)."""
        if self.stale_menus:
            element._driver = self
            element._generation = self._generation
        return element

    def _dropdown_trigger(self, menu):
        def action():
            menu["open"] = True
            self._bump_generation()

        label = menu.get("trigger", "More actions")
        # radix=False : bouton attrapé par le sélecteur large
        # `button[aria-expanded][data-state]` mais sans rien d'un menu d'actions
        attributes = {
            "aria-expanded": "true" if menu.get("open") else "false",
            "data-state": "open" if menu.get("open") else "closed",
            "innerHTML": ('<svg class="lucide lucide-ellipsis-vertical"></svg>' + label
                          if menu.get("radix", True) else label),
        }
        if menu.get("radix", True):
            attributes.update({"data-slot": "dropdown-menu-trigger",
                               "aria-haspopup": "menu"})

        return self._tracked(
            FakeElement("button", attributes, text=label, on_click=action))

    def _menu_item(self, menu, text):
        def action():
            menu["open"] = False
            self._bump_generation()
            if "slot" in text.lower() or "continue to slot" in text.lower():
                self.page = menu.get("page", "calendar")
                self.current_url = URLS.get(self.page, self.current_url)
                self.title = "Prendre rendez-vous"

        return self._tracked(FakeElement(
            "div",
            {"role": "menuitem", "data-slot": "dropdown-menu-item"},
            text=text,
            on_click=action,
        ))

    def _link_element(self, link):
        def action():
            self.page = link.get("page", "calendar")
            self.current_url = URLS.get(self.page, self.current_url)
            if link.get("logged_in") is not None:
                self.logged_in = link["logged_in"]

        return FakeElement(
            "a",
            {"href": link.get("href"), "aria-label": link.get("aria-label", "")},
            text=link.get("text", ""),
            on_click=action,
        )

    # ---- helpers de scénario ----

    def month_label(self):
        return MONTHS[self.month_index]

    def _available_day_elements(self):
        if self.page != "calendar":
            return []
        days = self.months_available.get(self.month_label(), [])
        elements = []
        for day in days:
            elements.append(
                FakeElement(
                    "td",
                    {"class": "rdp-day rdp-availability_high", "data-day": day},
                    text=day[-2:],
                    on_click=lambda d=day: self._clicked_days.append(d),
                )
            )
        return elements

    def _nav_button(self, direction):
        if direction == "next":
            enabled = self.month_index < len(MONTHS) - 1
            attrs = {"class": "rdp-button_previous rdp-nav_button_next",
                     "aria-label": f"Go to next month ({MONTHS[min(self.month_index + 1, len(MONTHS) - 1)]})"}
            action = self._go_next_month
        else:
            enabled = self.month_index > 0
            attrs = {"class": "rdp-button_previous rdp-nav_button_previous",
                     "aria-label": f"Go to previous month ({MONTHS[max(self.month_index - 1, 0)]})"}
            action = self._go_prev_month
        return FakeElement("button", attrs, on_click=action, enabled=enabled)

    def _go_next_month(self):
        if self.month_index < len(MONTHS) - 1:
            self.month_index += 1

    def _go_prev_month(self):
        if self.month_index > 0:
            self.month_index -= 1

    def _submit_login(self):
        self.logged_in = True
        self.page = "account"
        self.current_url = URLS["account"]
        self.title = "My Account"


class FakeWait:
    """Remplace WebDriverWait : évalue la condition immédiatement (test rapide)."""

    def __init__(self, driver, timeout, **kwargs):
        self.driver = driver
        self.timeout = timeout

    def until(self, condition, message=""):
        for _ in range(2):
            try:
                value = condition(self.driver)
                if value:
                    return value
            except NoSuchElementException:
                pass
            except Exception:
                pass
        raise TimeoutException(message or "attente expirée (fake)")
