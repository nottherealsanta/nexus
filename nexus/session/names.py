"""Versioned, ASCII-only human session identities (never editable labels).

Contract (plan 03 section 3.1): a name is ``adjective-colour-animal`` (each word
3-9 lowercase letters, at most 32 characters), assigned once to a logical
session and never reassigned. Uniqueness is the database's job
(``session_names.name`` is the primary key, across every project); the CSPRNG
draw only keeps allocation from being sequential.
The name lives in the ``sessions`` row and ``session_names`` table, not in the
log: no record is appended, so seqs stay stable. Exports carry it (a leading
``session.named`` line); import may keep an exported name when it is free.
``allocate`` runs inside the caller's ``BEGIN IMMEDIATE`` transaction.
"""
from __future__ import annotations

import re
import secrets
import sqlite3
import time

from ..errors import SessionError

# v2 vocabulary: 229 x 56 x 188 = 2,410,912 bases (~21.2 bits). A name is an
# identity handle, not a security token. Global UNIQUE(name) is the guarantee:
# on conflict we redraw up to 16 times, then try ``-2``..``-99`` on the first
# draw, then fail loudly. Retired names stay in ``session_names`` and are never
# reused. Changing the lists bumps NAMES_VERSION; existing names are unaffected.
# Every word is lowercase ASCII, 3-9 letters (so even a ``-99`` suffix fits 32 characters), unique across the three lists,
# and free of offensive, confusable, brand, or command-like words
# (tests/test_session_names.py lints this).
NAMES_VERSION = 2
ADJECTIVES = tuple(
    """
    agile alert alpine ample ancient ardent aspen astute atomic autumn avid balmy birch bold
    brave breezy bright brisk bubbly calm candid careful cedar cheery civil clever clover
    coastal cosmic cozy crisp curious dainty dancing dapper daring dashing dazzling deft desert
    devoted dewy distant dreamy drifting driven dusty eager earnest elegant epic even exact fair
    faithful fancy fearless fiery firm floating fluent foggy fond forest frank fresh friendly
    frosty gallant gentle gilded glad gleaming gliding glowing grand granite grateful harbor
    hardy hazy hearty helpful hilltop honest humble humming icy ideal island jaunty jolly joyful
    jubilant keen kind knowing lakeside lively lofty loyal lucid lucky lunar lush magic major
    maple marble meadow mellow merry mighty mindful misty modest mossy nifty nimble noble novel
    oaken optimal orchard orderly patient peppy placid playful plucky polished polite pretty
    prime proud prudent quaint quick quiet radiant rainy rapid rare ready regal relaxed
    riverside robust rolling rosy royal rustic sage sandy savvy serene settled shining shiny
    silent silken simple sincere sleek smooth snowy snug soaring solar solid sparkling spry
    stable starry stately steady stellar sterling still stormy stout sturdy suave subtle summer
    sunlit sunny super supple sure sweet tame tender thorough thrifty tidy timely tiny tough
    tranquil trim trusty upbeat urban valiant vast velvet verdant vibrant vital vivid wandering
    warm wavy whistling wild willow windy winning winter wise witty woolly worthy young zany
    zealous zesty zippy
    """.split()
)
COLORS = tuple(
    """
    amber azure beige black blue bronze brown charcoal chestnut cobalt copper coral cream
    crimson cyan denim emerald fuchsia ginger golden green hazel indigo ivory jade khaki
    lavender lemon lilac lime magenta maroon mauve mint navy ochre olive peach pearl pink plum
    purple rose russet saffron scarlet sepia silver slate tan teal turquoise umber violet white
    yellow
    """.split()
)
ANIMALS = tuple(
    """
    alpaca anchovy antelope armadillo avocet badger bandicoot barracuda beaver bison bittern
    bobcat buffalo bunting camel capybara caribou carp chamois cheetah chickadee chipmunk civet
    condor cormorant cougar cowrie coyote crane cricket crow cuckoo curlew deer dingo
    dormouse dove dragonfly duck dugong dunlin eagle egret eland elk emu ermine ferret finch
    flamingo fox frog fulmar gannet gazelle gerbil gibbon giraffe goat godwit goose gopher
    grouper grouse gull guppy halibut hamster hare harrier hawk hedgehog heron herring hippo
    hornet horse ibex ibis iguana impala jackal jay jellyfish kestrel kiwi koala kudu
    lark lemur leopard limpet linnet lion lizard lobster loon lorikeet macaw mackerel magpie
    manatee marlin marten meerkat mink minnow mole mongoose moose moth narwhal newt nuthatch
    ocelot octopus okapi orca oriole osprey ostrich otter owl oyster panther parrot partridge
    pelican penguin perch petrel pigeon pike pipit plover porpoise possum puffin quail quokka
    rabbit raven redwing reindeer rhino robin sandpiper seahorse seal serval shrike
    siskin skink skylark sloth snipe sparrow squid squirrel starfish starling stilt stoat stork
    sturgeon swallow swan tapir tern thrush tiger toucan trout tuna turtle urchin vole vulture
    wallaby walrus warbler weasel whale wolf wombat wren yak zebra
    """.split()
)
BASE_NAME_CAPACITY = len(ADJECTIVES) * len(COLORS) * len(ANIMALS)
_PATTERN = re.compile(r"[a-z]{3,9}-[a-z]{3,9}-[a-z]{3,9}(?:-(?:[2-9]|[1-9][0-9]))?", re.ASCII)
_REDRAWS = 16
_MAX_SUFFIX = 99


def valid_name(value: str) -> bool:
    return isinstance(value, str) and len(value) <= 32 and _PATTERN.fullmatch(value) is not None


def draw_name() -> str:
    return "-".join(secrets.choice(words) for words in (ADJECTIVES, COLORS, ANIMALS))


def allocate(conn: sqlite3.Connection, project: str, namespace: str, identity: str, *, preferred: str | None = None) -> str:
    """Name ``identity`` (idempotent); only call inside its ``BEGIN IMMEDIATE`` transaction.

    ``preferred`` (a name carried by an imported log or export) is tried first when
    valid; if it is taken a fresh name is allocated.
    """
    row = conn.execute(
        "SELECT logical_session_id FROM sessions "
        "WHERE project_id=? AND namespace=? AND id=?", (project, namespace, identity)).fetchone()
    if row is None:
        raise SessionError("Cannot name a session that does not exist")
    logical_id = row[0] or identity
    existing = conn.execute(
        "SELECT name FROM session_names WHERE project_id=? AND namespace=? AND logical_id=? AND retired_at IS NULL",
        (project, namespace, logical_id)).fetchone()
    if existing:
        return str(existing[0])
    base = draw_name()
    candidates = ([preferred] if preferred and valid_name(preferred) else []) + [base] + [draw_name() for _ in range(_REDRAWS - 1)]
    candidates += [f"{base}-{suffix}" for suffix in range(2, _MAX_SUFFIX + 1)]
    for name in candidates:
        try:
            conn.execute(
                "INSERT INTO session_names(name,project_id,namespace,logical_id,created_at) VALUES(?,?,?,?,?)",
                (name, project, namespace, logical_id, time.time()))
        except sqlite3.IntegrityError:
            continue
        conn.execute("UPDATE sessions SET name=? WHERE project_id=? AND namespace=? AND id=?",
                     (name, project, namespace, identity))
        return name
    raise SessionError("Session name space exhausted after retries")
