"""Migração das chaves Redis do esquema antigo (partilhado) para o esquema por unit.

Antes, o `platform` que entrava na chave era o session_id ou o nome da guild, o que
misturava salas: `history:discord_111` não sabia a que guild pertencia, `persona:discord`
era global, e os rate limits eram `rl:discord:{id}` para todas as salas do Discord.
O novo esquema endereça tudo por unit (ver scope.py).

Este script copia o que é inequivocamente transladável e NÃO apaga nada fora do que
está explicitamente listado. Não corre no arranque — é manual e idempotente:

    python migrate_units.py            # dry-run: só mostra o que faria
    python migrate_units.py --apply    # escreve

## Formatos antigos (verificados em bak/storage.py e nos call sites de bak/)

    history:{sid}          discord_111 | twitch_primisc_bob
    summary:{sid}          idem
    persona:{platform}     discord | discord:{gid} | twitch:{canal}
    memory:{user_key}      discord:{gid}:{name} | twitch:{canal}:{name}
    rl:{platform}:{user}   discord:{author_id} | twitch_{canal}:{author}
    rlh:{platform}:{user}  idem
    stats:total | stats:model:{m} | stats:user:{platform}:{user}

## O que NAO se pode migrar, e porque

Histórico/resumo do Discord: `history:discord_111` só regista o channel_id, e a unit
nova precisa da guild. Traduzir sem a saber daria `discord:dm:111` — uma unit que não
corresponde a sala nenhuma, onde nada a volta a ler. Deixados como estão.

Persona do Discord: existia `persona:discord` (global, do prefixo — nunca pertenceu a uma
sala) e `persona:discord:{gid}` (por guild, não por sala). Nenhum dos dois se traduz
para `persona:discord:{gid}:{channel}`.

Memória do Discord: `memory:discord:{gid}:{name}` — por guild e por nome de utilizador.
A unit nova acrescenta o channel_id e usa o id estável do utilizador, e o nome não diz
qual sala escreveu a entrada. Deixada como está.

Rate limits antigos: `rl:discord:{id}` contabilizava o mesmo utilizador em todas as salas.
Não há forma de saber onde foram gastos os pedidos, portanto recomeçam a zero.

## O que NAO precisa de migração

    persona:twitch:{canal}         -> é já persona:{unit} de twitch:{canal}
    memory:twitch:{canal}:{nome}   -> é já memory:{unit}:{user_key} (o user_key continua
                                     a ser author.name na Twitch)

Ambas as chaves mantêm o mesmo texto, por isso continuam a ser lidas sem tocar em nada.
"""
import argparse
import asyncio
import json
import sys

import config  # noqa: F401  (carrega o .env, obrigatorio antes de state)
import scope
import state


# ─────────────── helpers ───────────────

def _segments(key: str) -> list:
    return key.split(":")


def _legacy_unit(suffix: str, guild_map=None):
    """Unit correspondente a um session_id antigo, ou None se nao for determinavel."""
    try:
        if suffix.startswith("twitch_"):
            ch = suffix.split("_", 2)[1]
            return None if ch == "_" else scope.twitch_unit(ch)
        if suffix.startswith("discord_"):
            cid = suffix.split("_", 1)[1]
            gid = (guild_map or {}).get(cid)
            # Sem guild conhecida nao se inventa 'dm': seria uma unit errada.
            return scope.discord_unit(gid, cid) if gid is not None else None
    except Exception:
        return None
    return None


def _report(key: str, why: str, acc: dict):
    acc["skipped"] += 1
    print(f"  DEIXA   {key}\n           {why}", flush=True)


# ─────────────── 1. history / summary ───────────────

async def _copy_conversation(r, prefix, keyfn, stats, dry_run, guild_map):
    """Copia history/summary antigos para a unit. Twitch e determinavel; Discord nao."""
    collisions = {}
    async for old in r.scan_iter(match=f"{prefix}:*", count=100):
        suffix = old.split(":", 1)[1]
        # `prefix:*` tambem apanha as chaves JA migradas (o sufixo passou a usar ':'
        # em vez de '_': twitch:canal, discord:gid:cid). Nao sao candidatas.
        if suffix.startswith("twitch:") or suffix.startswith("discord:"):
            continue
        unit = _legacy_unit(suffix, guild_map)
        if not unit:
            _report(old, "sem guild conhecida — o esquema antigo so guardava o channel_id",
                    stats)
            continue
        collisions.setdefault(unit, []).append(old)
        new = keyfn(unit)
        if await r.exists(new):
            continue
        val = await r.get(old)
        if not val:
            continue
        if not dry_run:
            await r.set(new, val, ex=86400)
        stats["copied"] += 1
        print(f"  COPIA   {old}  ->  {new}", flush=True)
    # Na Twitch havia um historico por utilizador e agora ha um por canal: as chaves
    # nao se traduzem uma-a-uma. Dizemos quantas havia, em vez de fingir que deu certo.
    for unit, keys in collisions.items():
        if len(keys) > 1:
            print(f"  AVISO   {unit}: {len(keys)} chaves antigas disputam a mesma unit; "
                  f"so a primeira foi copiada (historico por utilizador -> por canal)",
                  flush=True)
            stats["lost"] += len(keys) - 1


# ─────────────── 2. stats ───────────────

async def _copy_stats(r, stats, dry_run, drop_old: bool = False):
    """stats:total / :model: / :user:  ->  hash stats:_global.

    Com drop_old, apaga tambem as chaves antigas — mas so depois de confirmar que o
    hash novo ficou escrito com os mesmos numeros. As unidades novas (stats:twitch:canal,
    stats:discord:g:c, stats:_global) nunca entram em 'legacy': so as de formato antigo,
    cujo segundo segmento e total/model/user."""
    new = scope.k_stats(scope.GLOBAL)
    if await r.exists(new):
        _report(new, "ja existe em migrate — o agregado global nao e reescrito", stats)
        return
    fields, models, users, legacy = {}, 0, 0, []
    total = await r.get("stats:total")
    if total:
        fields["total"] = int(total)
        legacy.append("stats:total")
    async for k in r.scan_iter(match="stats:model:*", count=100):
        # stats:model:<modelo> — o modelo pode ter '/', nunca ':'
        model = k.split(":", 2)[2]
        fields[f"model:{model}"] = int(await r.get(k) or 0)
        legacy.append(k)
        models += 1
    async for k in r.scan_iter(match="stats:user:*", count=100):
        # stats:user:{platform}:{user}  (platform = 'discord' ou 'twitch_{canal}')
        parts = _segments(k)
        if len(parts) != 4 or parts[1] != "user":
            _report(k, "formato inesperado para stats:user — descartado em vez de adivinhado",
                    stats)
            continue
        user = parts[3]
        fields[f"user:{user}"] = fields.get(f"user:{user}", 0) + int(await r.get(k) or 0)
        legacy.append(k)
        users += 1
    if not fields:
        print("  (nenhuma chave stats:* antiga)", flush=True)
        return
    if not dry_run:
        try:
            await r.hset(new, mapping=fields)
        except Exception as e:
            # Fail-soft como o resto do codebase: se o Redis cair a meio, as stats
            # antigas TEM de sobreviver. Por isso nao se apaga nada neste caminho.
            _report(new, f"escrita falhou ({type(e).__name__}) — stats antigas conservadas",
                    stats)
            return
    stats["copied"] += 1
    print(f"  COPIA   stats:*  ->  {new}  "
          f"(total={fields.get('total', 0)}, {models} modelos, {users} utilizadores)", flush=True)
    if not drop_old:
        return

    # As chaves antigas ficam orfas para sempre (foram feitas com incr, sem expire).
    # So se apagam DEPOIS de confirmar que o hash novo existe e tem os mesmos numeros —
    # caso contrario um erro a meio deixava o bot sem nenhum historico de stats.
    written = await r.hgetall(new)
    if fields.get("total") and written.get("total") != str(fields["total"]):
        _report(new, "o hash novo nao bate certo — as stats antigas NAO sao apagadas", stats)
        return
    if dry_run:
        print(f"  (com --drop-old-stats: apagaria {len(legacy)} chaves stats:* orfas)", flush=True)
        return
    for k in legacy:
        await r.delete(k)
        stats["dropped"] += 1
        print(f"  APAGA   {k}  (orfa: substituida pelo hash {new})", flush=True)


# ─────────────── 3. rate limits antigos ───────────────

async def _drop_rate_limits(r, stats, dry_run):
    """Apaga apenas os rate limits de formato ANTIGO.

    O padrao `rl:*` tambem casaria com as chaves novas (`rl:{unit}:{user}`, 4-5
    segmentos) e apagaria os limites vivos num segundo `--apply`. Por isso filtramos
    pelo numero de segmentos: antigo = 3 (`rl:discord:123`, `rl:twitch_canal:bob`),
    novo = 4 (twitch) ou 5 (discord)."""
    old_shapes = set()
    async for k in r.scan_iter(match="rl:*", count=100):
        old_shapes.add(k)
    async for k in r.scan_iter(match="rlh:*", count=100):
        old_shapes.add(k)
    dropped = kept = 0
    for k in sorted(old_shapes):
        if len(_segments(k)) == 3:
            if not dry_run:
                await r.delete(k)
            dropped += 1
            print(f"  APAGA   {k}  (rate limit global antigo, nao transladavel)", flush=True)
        else:
            kept += 1
    if kept:
        print(f"  MANTEM  {kept} chaves rl/rlh ja no formato novo (formato da unit)", flush=True)
    stats["dropped"] += dropped
    stats["kept"] += kept


# ─────────────── 4. o que fica para tras ───────────────

async def _report_unmigratable(r, stats):
    """Lista o que se perde, para o operador ver antes de aceitar."""
    groups = [
        ("memory:discord:*", "memoria por guild+nome; a unit nova quer channel+id"),
        ("persona:discord", "persona global do prefixo — nunca pertenceu a uma sala"),
        ("persona:discord:*", "persona por guild, nao por sala"),
    ]
    for pattern, why in groups:
        n = 0
        async for k in r.scan_iter(match=pattern, count=100):
            _report(k, why, stats)
            n += 1
        if n > 1:
            print(f"  ...     (+{n - 1} mais com o mesmo padrao)", flush=True)
    # Twitch: chaves que ja estao no formato novo, so para confirmar que existem.
    for pattern, note in (
        ("persona:twitch:*", "ja e persona:{unit} — nenhuma migracao necessaria"),
        ("memory:twitch:*", "ja e memory:{unit}:{user_key} — nenhuma migracao necessaria"),
    ):
        n = 0
        async for _ in r.scan_iter(match=pattern, count=100):
            n += 1
        if n:
            print(f"  OK      {n} chaves {pattern} — {note}", flush=True)


# ─────────────── orchestration ───────────────

async def migrate(dry_run: bool = True, guild_map=None, drop_old_stats: bool = False) -> dict:
    r = state.redis_client
    stats = {"copied": 0, "dropped": 0, "skipped": 0, "kept": 0, "lost": 0}
    print("== 1. history / summary ==", flush=True)
    await _copy_conversation(r, "history", scope.k_history, stats, dry_run, guild_map)
    await _copy_conversation(r, "summary", scope.k_summary, stats, dry_run, guild_map)
    print("== 2. stats ==", flush=True)
    await _copy_stats(r, stats, dry_run, drop_old_stats)
    print("== 3. rate limits antigos ==", flush=True)
    await _drop_rate_limits(r, stats, dry_run)
    print("== 4. nao migravel ==", flush=True)
    await _report_unmigratable(r, stats)

    print("", flush=True)
    print(f"copiados={stats['copied']}  apagados={stats['dropped']}  "
          f"mantidos={stats['kept']}  inalterados={stats['skipped']}  "
          f"perdidos(colisao twitch)={stats['lost']}", flush=True)
    print("(dry-run: nada foi escrito)" if dry_run else "(APLICADO)", flush=True)
    if dry_run:
        print("\nPara aplicar: python migrate_units.py --apply", flush=True)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Migra chaves Redis para o esquema por unit")
    ap.add_argument("--apply", action="store_true",
                    help="escreve de facto (default e dry-run, so mostra)")
    ap.add_argument("--drop-old-stats", action="store_true",
                    help="apaga as chaves stats:* antigas depois de confirmar que o "
                         "hash novo ficou escrito (so com --apply)")
    ap.add_argument("--with-history", action="store_true",
                    help="traduz tambem o history/summary do Discord usando "
                         "scope._guild_cache (so funciona dentro do processo do bot)")
    args = ap.parse_args()

    gm = dict(scope._guild_cache) if args.with_history and scope._guild_cache else None
    if args.with_history and not gm:
        print("AVISO: sem guild_map carregada — history/summary do Discord nao sao "
              "traduzidos (e nao o serao: ver docstring).", flush=True)
    asyncio.run(migrate(dry_run=not args.apply, guild_map=gm,
                          drop_old_stats=args.drop_old_stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
