"""Comandi: python -m bnbmonitor <comando>."""
from __future__ import annotations

import argparse
import logging
import sys

from . import config, db


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bnbmonitor", description="Monitor prezzi BnB Trieste")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scheduler", help="pianificatore: raccolta notturna e riepilogo mattutino")
    run = sub.add_parser("run", help="esegue subito il ciclo notturno")
    run.add_argument("--no-backup", action="store_true")
    sub.add_parser("init-db", help="crea o aggiorna lo schema del database")
    sub.add_parser("summary", help="invia subito il riepilogo Telegram")
    sub.add_parser("print-summary", help="mostra il riepilogo senza inviarlo")
    sub.add_parser("backup", help="esporta il database nella cartella di backup")
    sub.add_parser("test-telegram", help="invia un messaggio di prova")
    sub.add_parser("reclassify", help="ricalcola poligono, distanze e segmenti dopo un cambio di configurazione")
    sub.add_parser("refresh-metrics", help="ricalcola tutte le metriche precalcolate")
    sub.add_parser("check-config", help="verifica la configurazione e mostra i parametri derivati")
    demo = sub.add_parser("seed-demo", help="inserisce dati sintetici (solo database vuoto)")
    demo.add_argument("--days", type=int, default=21)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    cfg = config.load()

    if args.cmd == "scheduler":
        from . import scheduler
        scheduler.loop(cfg)
    elif args.cmd == "run":
        from . import pipeline
        result = pipeline.run_nightly(cfg, do_backup=not args.no_backup)
        print(result)
        return 0 if result["status"] == "valid" else 1
    elif args.cmd == "check-config":
        from datetime import date

        from . import schedule
        print(f"Profili: {', '.join(f'{p.key} ({p.label})' for p in cfg.profiles.values())}")
        print(f"Vertici del poligono: {len(cfg.polygon)}")
        print(f"Punto di ricerca: {cfg.search_point[0]:.5f}, {cfg.search_point[1]:.5f}")
        print(f"Raggio di arresto: {cfg.stop_radius_m:.0f} m")
        for p in cfg.profiles.values():
            print(f"Ricerche stanotte per {p.key}: {len(schedule.dates_to_check(p, date.today()))}")
        print(f"Telegram configurato: {'sì' if cfg.telegram_token and cfg.telegram_chat_id else 'no'}")
    else:
        conn = db.connect(cfg.database_url)
        db.apply_schema(conn)
        if args.cmd == "init-db":
            print("Schema aggiornato.")
        elif args.cmd == "summary":
            from . import summary
            summary.send_morning(conn, cfg)
        elif args.cmd == "print-summary":
            from . import summary
            print(summary.build_summary(conn, cfg))
        elif args.cmd == "backup":
            from . import backup
            print(backup.run_backup(cfg))
        elif args.cmd == "test-telegram":
            from . import notify
            ok = notify.send(cfg, "✅ Monitor BnB Trieste: il bot funziona.", kind="test", conn=conn)
            return 0 if ok else 1
        elif args.cmd == "reclassify":
            from . import pipeline
            pipeline.reclassify_all(conn, cfg)
            pipeline.refresh_metrics(conn)
            print("Fatto.")
        elif args.cmd == "refresh-metrics":
            from . import pipeline
            pipeline.refresh_metrics(conn)
            print("Fatto.")
        elif args.cmd == "seed-demo":
            from . import demo
            demo.seed(cfg, days=args.days)
            print("Dati demo inseriti.")
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
