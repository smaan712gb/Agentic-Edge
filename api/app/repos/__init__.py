"""Repository layer — async CRUD over the ORM, the only thing routes touch.

Routes never see SQLAlchemy directly. Repos wrap query patterns we
actually use, so adding new ones is intentional and reviewable, and
N+1 selectinload patterns live in one place.
"""

from .events import EventRepo
from .runs import RunRepo
from .themes import ThemeRepo

__all__ = ["EventRepo", "RunRepo", "ThemeRepo"]
