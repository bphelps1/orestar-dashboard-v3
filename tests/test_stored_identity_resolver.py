"""The resolver must not physically reapply canonical admin merges."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scraper'))
import resolve_donors as resolver

class DB:
    def __init__(self, stored): self.stored=stored
    def cursor(self): return self
    def execute(self,sql): pass
    def fetchall(self): return [('a','b','merged'),('c','d','separate')]
    def fetchone(self): return (self.stored,)

def test_stored_identity_imports_leave_admin_merges_to_the_database():
    must,cannot=resolver.load_alias_constraints(DB(True))
    assert must == []
    assert cannot == {frozenset(('c','d'))}

def test_legacy_imports_keep_working_before_migration():
    must,cannot=resolver.load_alias_constraints(DB(False))
    assert must == [('a','b')]
    assert cannot == {frozenset(('c','d'))}
