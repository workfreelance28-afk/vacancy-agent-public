import inspect

from vacancy_agent.domain.ports import TelegramNotifier


def test_telegram_notifier_methods_are_async() -> None:
    assert inspect.iscoroutinefunction(TelegramNotifier.send_vacancy_card)
    assert inspect.iscoroutinefunction(TelegramNotifier.handle_callback)
