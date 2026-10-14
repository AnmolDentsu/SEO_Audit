import argparse
from app import models
from app.database import SessionLocal


def seed_demo():
    db = SessionLocal()
    try:
        p = db.query(models.Project).filter_by(name="Demo SEO comparison").first()
        if p:
            print(f"Demo already exists: project id={p.id}"); return
        p = models.Project(name="Demo SEO comparison", client_name="Example Client")
        db.add(p); db.flush()
        db.add_all([
            models.Target(project_id=p.id, url="https://example.com/", role="CLIENT", label="Client"),
            models.Target(project_id=p.id, url="https://www.iana.org/help/example-domains", role="P0", label="Competitor"),
            models.Keyword(project_id=p.id, keyword="example domain", priority="P0", target_min=0.1, target_max=2.0),
        ])
        db.commit(); print(f"Created demo project id={p.id}")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed-demo"])
    args = parser.parse_args()
    if args.command == "seed-demo": seed_demo()


if __name__ == "__main__": main()
