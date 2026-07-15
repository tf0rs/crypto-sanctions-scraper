from sqlalchemy import Column, DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class SanctionedEntity(Base):
    __tablename__ = "sanctioned_entities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(Text, nullable=False)  # e.g. "OFAC_SDN", "EU_CONSOLIDATED", "UK_OFSI"
    source_id = Column(Text, nullable=False)  # the source's own entity id (e.g. SDN ent_num)
    name = Column(Text, nullable=False)
    entity_type = Column(Text)  # individual / entity / vessel / aircraft
    programs = Column(Text)  # source's sanctions program codes, e.g. "CYBER2] [DPRK3"
    remarks = Column(Text)
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now())

    addresses = relationship("CryptoAddress", back_populates="entity")

    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_entity_source"),)


class CryptoAddress(Base):
    __tablename__ = "crypto_addresses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_id = Column(Integer, ForeignKey("sanctioned_entities.id"), nullable=False)
    address = Column(Text, nullable=False)
    chain = Column(Text)  # e.g. XBT, ETH, USDT, TRX, XMR

    entity = relationship("SanctionedEntity", back_populates="addresses")

    __table_args__ = (UniqueConstraint("entity_id", "address", name="uq_address_entity"),)


class PressRelease(Base):
    __tablename__ = "press_releases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(Text, nullable=False)  # e.g. "DOJ", "FINCEN", "OFAC_PRESS", "EUROPOL"
    external_id = Column(Text)  # source's own id/uuid, if it has one
    title = Column(Text, nullable=False)
    url = Column(Text, nullable=False, unique=True)
    published_at = Column(DateTime(timezone=True))
    matched_keywords = Column(Text)  # comma-separated keywords that triggered inclusion
    raw_markdown = Column(Text)  # full page/body content, converted to markdown
    scraped_at = Column(DateTime(timezone=True), server_default=func.now())


class ScraperCheckpoint(Base):
    __tablename__ = "scraper_checkpoints"

    scraper_name = Column(Text, primary_key=True)
    checkpoint = Column(Text)  # opaque value (page number, cursor, id) — meaning is source-specific
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
