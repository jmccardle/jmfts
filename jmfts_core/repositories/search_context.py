"""Search Context Repository - CRUD for named search presets"""

from typing import Optional
from sqlalchemy import select
from sqlalchemy.orm import Session

from jmfts_core.models.search_context import SearchContext


class SearchContextRepository:
    """Repository for search context CRUD operations"""

    def __init__(self, session: Session):
        self.session = session

    def create(
        self,
        name: str,
        config: dict,
        description: Optional[str] = None,
    ) -> SearchContext:
        ctx = SearchContext(name=name, config=config, description=description)
        self.session.add(ctx)
        self.session.flush()
        return ctx

    def get_by_name(self, name: str) -> Optional[SearchContext]:
        return self.session.execute(
            select(SearchContext).where(SearchContext.name == name)
        ).scalar_one_or_none()

    def get_by_id(self, context_id: int) -> Optional[SearchContext]:
        return self.session.get(SearchContext, context_id)

    def list_all(self) -> list[SearchContext]:
        return list(self.session.execute(select(SearchContext)).scalars().all())

    def update(
        self,
        name: str,
        config: Optional[dict] = None,
        description: Optional[str] = ...,
    ) -> Optional[SearchContext]:
        ctx = self.get_by_name(name)
        if not ctx:
            return None
        if config is not None:
            ctx.config = config
        if description is not ...:
            ctx.description = description
        self.session.flush()
        return ctx

    def delete(self, name: str) -> bool:
        ctx = self.get_by_name(name)
        if not ctx:
            return False
        self.session.delete(ctx)
        self.session.flush()
        return True

    def resolve_params(self, context_name: str, overrides: dict) -> dict:
        """Resolve a context preset into search parameters, with overrides.

        Returns a dict of search parameters where explicit overrides take
        precedence over context defaults.
        """
        ctx = self.get_by_name(context_name)
        if not ctx:
            raise ValueError(f"Search context not found: {context_name}")

        params = dict(ctx.config)
        # Apply overrides — only non-None values override context defaults
        for key, value in overrides.items():
            if value is not None:
                params[key] = value
        return params
