import sys
sys.path.insert(0, 'bot')
from main import Bot, configure_logging
configure_logging()

bot = Bot(install_signal_handlers=False)
fake_item = {
    "analyzed_at": "test-event-002",
    "tweet": {"text": "BREAKING: Federal Reserve just announced emergency 50bps rate cut effective immediately"},
    "urgency": "high",
}
bot.handle_feed_item(fake_item)