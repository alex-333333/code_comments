import sys
from pathlib import Path

# Скрипты проекта лежат в корне плоско (parser.py, metrics.py ...): добавляем корень в sys.path,
# чтобы тесты импортировали их так же, как их импортируют сами скрипты.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
