"""One live Telegram status card; cosmetic updates never stop ticket work."""
from html import escape
import logging
from random import choice
import threading
import time

log = logging.getLogger("bilet-bot.progress")

PDF_STEPS = ("Получаю файл", "Перепечатываю билет", "Проверяю коды и данные", "Отправляю результат")
BATCH_STEPS = ("Получаю билеты", "Перепечатываю пачку", "Собираю ZIP", "Отправляю результат")
COLLECT_STEPS = ("Получаю файл", "Добавляю в пачку")
FRAMES = ("◐", "◓", "◑", "◒")
DED_LINES = {
    "Получаю файл": (
        "Дед вставил лист в машинку. Печатает заново.",
        "Лента новая, краска свежая. Давай билет.",
        "Дед надел очки и читает твой билет.",
    ),
    "Получаю билеты": (
        "Пачка на столе. Дед разминает пальцы.",
        "Билетов много, а дед один. Зато какой.",
    ),
    "Перепечатываю билет": (
        "Машинка стучит: лишние буквы — в корзину.",
        "Дед перепечатывает билет начисто. Клякс не остаётся.",
        "Старые строки вычёркиваем. Чисто, как в первый раз.",
        "Печатает так, будто Пушкин смотрит через плечо.",
    ),
    "Проверяю коды и данные": (
        "Дед сверяет каждую букву с оригиналом.",
        "Коды проверены дважды. Дед ошибок не прощает.",
        "Стучит машинка — сверяются коды и места.",
        "Каждая строка — как пушкинская: ни лишней буквы.",
    ),
    "Отправляю результат": (
        "Дед дует на чернила. Отправляю билет.",
        "Из машинки — прямо в чат. Не мято, не рвано.",
        "Билет перепечатан. Дед доволен.",
    ),
    "Перепечатываю пачку": (
        "Дед печатает всю пачку. По одному листу.",
        "Пачка на машинке. Очередь подходит.",
    ),
    "Собираю ZIP": (
        "Дед перевязывает стопку бечёвкой. Выходит ZIP.",
        "Листы — в папку, отчёт — сверху. Как в архиве.",
    ),
    "Добавляю в пачку": (
        "Кладу лист в стопку. Пачка растёт.",
        "Дед подкалывает билет к остальным.",
    ),
}
DED_DONE_LINES = (
    "Перепечатано начисто. Ни кляксы, ни лишней буквы ✍️",
    "Дед снял очки и полюбовался. Билет готов ✍️",
    "Машинка стучала не зря. Билеты как новые ✍️",
    "Начисто, без помарок. Дед подписал работу ✍️",
    "Дед закончил. Можно идти в театр ✍️",
    "Перепечатано так чисто, будто сам Александр Сергеевич правил ✍️",
)


class Progress:
    """Transport receives Bot API method/kwargs and returns the JSON envelope."""

    def __init__(self, chat, name, transport, steps=PDF_STEPS, interval=2.5, cleanup=None):
        self.chat = chat
        self.name = " ".join(str(name).split())[:96]
        self.transport = transport
        self.cleanup = cleanup
        self.steps = steps
        # Choose once per stage: a timer refresh must not flicker the wording.
        self.jokes = tuple(choice(DED_LINES.get(label, DED_LINES["Получаю файл"]))
                           for label in steps)
        self.done_joke = choice(DED_DONE_LINES)
        self.interval = interval
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        self.message_id = None
        self.stage = 0
        self.detail = "Дед уже стучит по машинке. Билеты перепечатываются."
        self.outcome = None
        self.total = self.completed = self.failed = 0
        self.frame = 0

    def update(self, stage, detail=None):
        with self.lock:
            if self.outcome:
                return
            self.stage = max(0, min(stage, len(self.steps) - 1))
            if detail is not None:
                self.detail = detail

    def batch(self, completed, total, failed=0):
        with self.lock:
            self.completed, self.total, self.failed = completed, total, failed

    def finish(self, detail, failed=False):
        with self.lock:
            self.outcome = "error" if failed else "done"
            self.detail = detail
        self.wake.set()

    def render(self):
        with self.lock:
            stage, detail, outcome = self.stage, self.detail, self.outcome
            completed, total, failed, frame = self.completed, self.total, self.failed, self.frame
        title = {None: "✍️ ДЕД РЕДАЧИТ · Перепечатываю билеты", "done": "✍️ ДЕД РЕДАЧИТ · Всё перепечатано",
                 "error": "⚠️ ДЕД РЕДАЧИТ · Машинка заела"}[outcome]
        lines = [f"<b>{title}</b>", f"<code>{escape(self.name)}</code>", ""]
        for index, label in enumerate(self.steps):
            if outcome == "done" or index < stage:
                icon = "✓"
            elif index == stage:
                icon = "!" if outcome == "error" else FRAMES[frame % len(FRAMES)]
            else:
                icon = "○"
            lines.append(f"{icon} {label}")
        if total:
            filled = min(10, completed * 10 // total)
            lines += ["", "▰" * filled + "▱" * (10 - filled),
                      f"Обработано: {completed} из {total} · ошибок: {failed}"]
        seconds = max(0, int(time.monotonic() - self.started))
        lines += ["", escape(str(detail)[:500])]
        if outcome != "error" and not failed:
            lines += ["", escape(self.done_joke if outcome == "done" else self.jokes[stage])]
        lines += [f"⏱ {seconds // 60:02d}:{seconds % 60:02d}"]
        return "\n".join(lines)

    def _request(self, method, **payload):
        try:
            response = self.transport(method, **payload)
            return response if isinstance(response, dict) else {}
        except Exception:
            # No exception text: transport errors may contain credentials.
            log.warning("Progress transport failed (%s)", method)
            return {}

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, name="ticket-progress", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        try:
            self._run_card()
        finally:
            if self.cleanup:
                self.cleanup()

    def _run_card(self):
        # Creation belongs to this cosmetic thread too: a slow sendMessage
        # must never delay download, cleanup or delivery of the actual ticket.
        response = self._request("sendMessage", chat_id=self.chat, text=self.render(),
                                 parse_mode="HTML", disable_notification=True)
        result = response.get("result")
        if response.get("ok") and isinstance(result, dict):
            self.message_id = result.get("message_id")
        if self.message_id is None:
            return
        delay = self.interval
        not_before = 0
        failures = 0
        while True:
            self.wake.wait(delay)
            self.wake.clear()
            # Completion wakes the renderer, but never overrides a Telegram
            # cooldown. Re-check after every wake, including finish().
            while time.monotonic() < not_before:
                self.wake.wait(not_before - time.monotonic())
                self.wake.clear()
            with self.lock:
                self.frame += 1
                finished = self.outcome is not None
            response = self._request("editMessageText", chat_id=self.chat, message_id=self.message_id,
                                     text=self.render(), parse_mode="HTML")
            if response.get("ok") or "message is not modified" in response.get("description", ""):
                failures = 0
                delay = self.interval
                if finished:
                    return
            else:
                failures += 1
                retry = (response.get("parameters") or {}).get("retry_after", 0)
                delay = max(self.interval, int(retry or 5 * failures))
                not_before = time.monotonic() + delay
                if failures >= 3 or response.get("error_code") in {400, 403}:
                    return
            # A completion during the API request is picked up immediately.
            with self.lock:
                if self.outcome is not None and not finished:
                    self.wake.set()

    def __exit__(self, exc_type, exc, traceback):
        with self.lock:
            unfinished = self.outcome is None
        if unfinished:
            self.finish("Не удалось завершить обработку. Отправь файл ещё раз.", failed=True)
        # Cosmetic network requests run independently of the processing worker.
        if self.thread:
            self.thread.join(timeout=0)
        return False
