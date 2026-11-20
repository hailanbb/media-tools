import sqlite3
import tempfile
import unittest
from pathlib import Path
import test_v2551_review_closure as f
from app.core.auxiliary_inventory import mark_verified_auxiliary_folders
from app.database import _configure_sqlite_connection

class Repairs(unittest.TestCase):
    def test_wal_allows_reader_and_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/"isolated.db")
            with sqlite3.connect(path) as a, sqlite3.connect(path) as b:
                _configure_sqlite_connection(a,None)
                _configure_sqlite_connection(b,None)
                a.execute("create table sample(value integer)");a.commit()
                a.execute("begin");a.execute("select * from sample").fetchall()
                b.execute("insert into sample values (1)");b.commit()
                self.assertEqual(a.execute("pragma journal_mode").fetchone()[0],"wal")
                a.rollback()
                self.assertEqual(a.execute("select count(*) from sample").fetchone()[0],1)
            a.close();b.close()

    def check_folder(self, children):
        class Disk:
            async def list_files(self,path,force_refresh=False):
                assert force_refresh
                return children
        return f.immediate(mark_verified_auxiliary_folders([{"name":"合并保留","path":"/src/合并保留","isDirectory":True}],Disk()))[0]

    def test_auxiliary_only_verified(self):
        self.assertTrue(self.check_folder([{"name":"season.nfo","path":"/src/合并保留/season.nfo"}])["verified_auxiliary_only"])

    def test_video_and_strm_never_hidden(self):
        for name in ["S01E01.mkv","S01E01.strm","unknown.bin"]:
            self.assertFalse(self.check_folder([{"name":name,"path":"/src/合并保留/"+name}])["verified_auxiliary_only"])

    def test_incomplete_read_not_empty(self):
        self.assertFalse(self.check_folder(None)["verified_auxiliary_only"])

    def test_outside_path_rejected(self):
        self.assertFalse(self.check_folder([{"name":"season.nfo","path":"/other/season.nfo"}])["verified_auxiliary_only"])

if __name__ == "__main__":unittest.main(verbosity=2)
