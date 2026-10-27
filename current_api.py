import logging
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

CONVERT_URL = "http://api.exchangerate.host/convert"
logger = logging.getLogger(__name__)


class ExchangeRateError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def fetch_json(url, params):
    response = requests.get(url, params=params, timeout=20)
    data = response.json()
    error = data.get("error") or {}
    if error.get("code") != 106:
        return data

    reset_at = response.headers.get("x-rate-limit-reset")
    if reset_at:
        wait_seconds = max(0.0, float(reset_at) - time.time()) + 0.2
    else:
        wait_seconds = 1.0
    time.sleep(wait_seconds)

    response = requests.get(url, params=params, timeout=20)
    return response.json()


def get_current_rate(default: str = "USD", currencies: list[str] | None = None):
    if currencies is None:
        currencies = ["EUR", "GBP", "JPY"]

    url = "https://api.exchangerate.host/live"
    params = {
        "access_key": os.getenv("CURRENCY_ACCESS_KEY"),
        "source": default,
        "currencies": ",".join(currencies),
    }
    return fetch_json(url, params)


def convert_currency(amount: float, from_currency: str, to_currency: str):
    params = {
        "access_key": os.getenv("CURRENCY_ACCESS_KEY"),
        "from": from_currency,
        "to": to_currency,
        "amount": amount,
    }
    return fetch_json(CONVERT_URL, params)


def convert_amount(amount: float, from_currency: str, to_currency: str) -> dict:
    """Конвертация через http://api.exchangerate.host/convert."""
    if not os.getenv("CURRENCY_ACCESS_KEY"):
        raise ExchangeRateError(
            "Не найден ключ API. Добавьте CURRENCY_ACCESS_KEY в файл .env."
        )

    try:
        source = from_currency.strip().upper()
        target = to_currency.strip().upper()
        value = float(amount)
    except (AttributeError, TypeError, ValueError):
        raise ExchangeRateError("Не получилось прочитать валюту или сумму.") from None

    if not re.fullmatch(r"[A-Z]{3}", source) or not re.fullmatch(r"[A-Z]{3}", target):
        raise ExchangeRateError("Код валюты должен состоять из трёх букв, например USD.")
    if value <= 0:
        raise ExchangeRateError("Сумма для конвертации должна быть больше нуля.")

    try:
        data = convert_currency(value, source, target)
    except requests.RequestException:
        raise ExchangeRateError(
            "Не удалось связаться с сервисом курсов. Проверьте интернет и попробуйте ещё раз."
        ) from None
    except ValueError:
        raise ExchangeRateError(
            "Сервис курсов вернул непонятный ответ. Попробуйте ещё раз."
        ) from None

    if not isinstance(data, dict):
        raise ExchangeRateError("Сервис курсов вернул непонятный ответ. Попробуйте ещё раз.")
    if data.get("success"):
        return _parsed_conversion(data, value, source, target)

    error = data.get("error") or {}
    code = error.get("code")
    if not isinstance(code, int):
        code = None
    logger.warning("convert failed, code=%s", code)
    raise ExchangeRateError(_friendly_api_error(code), code)


def _parsed_conversion(data: dict, amount: float, source: str, target: str) -> dict:
    try:
        result = float(data["result"])
        quote = data.get("info", {}).get("quote")
        rate = float(quote) if quote is not None else result / amount
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise ExchangeRateError(
            "Сервис курсов не прислал сумму конвертации. Попробуйте ещё раз."
        ) from None
    return {
        "amount": amount,
        "from": source,
        "to": target,
        "rate": rate,
        "result": result,
    }


def _friendly_api_error(code: int | None) -> str:
    if code == 101:
        return "Сервис не принял ключ доступа. Проверьте CURRENCY_ACCESS_KEY в файле .env."
    if code == 104:
        return "Месячный лимит запросов по ключу закончился. Сейчас курс получить нельзя."
    if code == 106:
        return "Слишком частые запросы к сервису курсов. Подождите пару секунд и повторите."
    if code in (201, 202):
        return "Эта валюта недоступна в сервисе курсов. Проверьте страну или введите другой код валюты."
    return "Сервис курсов не смог выполнить запрос. Попробуйте ещё раз чуть позже."


def get_all_supported_currencies():
    """
    Fetch all supported currencies from the exchangerate.host API.
    """
    url = "https://api.exchangerate.host/symbols"
    params = {
        "access_key": os.getenv("CURRENCY_ACCESS_KEY"),
    }
    return fetch_json(url, params)


if __name__ == "__main__":
    data = get_current_rate(default="RUB", currencies=["USD", "EUR", "GBP", "JPY", "CNY"])
    print(data)
    print(convert_currency(100, "USD", "EUR"))
