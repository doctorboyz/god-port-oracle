Python Enum Forward Reference in Pydantic Models

**Rule**: When adding an Enum field to a Pydantic model, the Enum class must be defined BEFORE the model class. Python evaluates class bodies top-to-bottom.

**Why**: Unlike type hints (which can use `from __future__ import annotations` for forward references), field DEFAULT values like `trading_mode: TradingMode = TradingMode.SWING` require the actual class at definition time. No forward reference possible.

**How to apply**: Define all enums before any models that use them. Group enums at the top of models.py, then models below. If adding a new enum used by an existing model, move the enum definition above that model.