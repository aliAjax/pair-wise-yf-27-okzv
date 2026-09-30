"""审查依据包、封存、失效、失败重试与持久化的测试。"""
import base64
import tempfile
import threading
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


def b64(text):
    return base64.b64encode(text.encode()).decode()


class BasisPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.store = ProvenanceStore(self.db)
        self.store.seed()
        self._setup()

    def tearDown(self):
        self.tmp.cleanup()

    def _setup(self):
        s = self.store
        self.obj = s.create_object("staff", "M-3001", "青铜器", "礼器", "市博物馆", "来源待核。")
        self.src = s.add_source("staff", "馆藏档案", "archive", "ACC-3001")
        self.ev = s.add_event("staff", self.obj["id"], "acquisition", "1999-07-01", "", "本市", "购入", self.src["id"], "public")
        self.evidence = s.upload_evidence("staff", self.obj["id"], "purchase.pdf", b64("purchase record"), "internal", self.ev["id"])
        self.claim = s.create_claim("claimant1", self.obj["id"], "王氏家族", "返还藏品")

    # ------------------------------------------------------------ 建包与封存

    def test_create_and_seal_records_version_and_digest(self):
        pkg = self.store.create_basis_package("reviewer1", self.claim["id"], [self.ev["id"]], [self.evidence["id"]])
        self.assertEqual(pkg["status"], "draft")
        job = self.store.seal_basis_package("reviewer1", pkg["id"])
        self.assertEqual(job["status"], "completed")
        sealed = self.store.get_basis_package("reviewer1", pkg["id"])
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["sealed_object_version"], 2)
        self.assertEqual(len(sealed["sealed_evidence_digest"]), 1)
        self.assertEqual(sealed["sealed_evidence_digest"][0]["sha256"], self.evidence["sha256"])
        self.assertEqual(sealed["sealed_evidence_digest"][0]["id"], self.evidence["id"])
        self.assertTrue(sealed["is_valid"])
        self.assertEqual(sealed["affected_items"], [])
        # 封存任务进度可查，步骤全部完成。
        fetched = self.store.get_job("reviewer1", job["id"])
        self.assertEqual(fetched["status"], "completed")
        self.assertTrue(all(s["status"] == "done" for s in fetched["steps"]))

    def test_cannot_seal_twice(self):
        pkg = self.store.create_basis_package("reviewer1", self.claim["id"], [self.ev["id"]], [self.evidence["id"]])
        self.store.seal_basis_package("reviewer1", pkg["id"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.seal_basis_package("reviewer1", pkg["id"])
        self.assertEqual(ctx.exception.code, "conflict")

    def test_rejects_non_public_event(self):
        internal_ev = self.store.add_event("staff", self.obj["id"], "note", "2000-01-01", "", "馆内", "内部说明", None, "internal")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_basis_package("reviewer1", self.claim["id"], [internal_ev["id"]], [])
        self.assertEqual(ctx.exception.code, "event_not_public")

    # ------------------------------------------------------------ 并发封存

    def test_concurrent_seal_first_writer_wins_latecomer_conflict(self):
        pkg = self.store.create_basis_package("reviewer1", self.claim["id"], [self.ev["id"]], [self.evidence["id"]])
        outcomes = {}

        def do_seal(tag):
            try:
                outcomes[tag] = ("ok", self.store.seal_basis_package("reviewer1", pkg["id"]))
            except BusinessError as exc:
                outcomes[tag] = ("err", exc.code)

        t1 = threading.Thread(target=do_seal, args=("a",))
        t2 = threading.Thread(target=do_seal, args=("b",))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(v[0] for v in outcomes.values()), ["err", "ok"])
        err_tag = next(k for k, v in outcomes.items() if v[0] == "err")
        self.assertEqual(outcomes[err_tag][1], "conflict")
        # 后到者的失败任务保留未完成项，可查。
        jobs = self.store.list_jobs("reviewer1", self.claim["id"])
        failed = [j for j in jobs if j["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("lock", failed[0]["error"]["unfinished"])
        # 先写入者封存成功。
        sealed = self.store.get_basis_package("reviewer1", pkg["id"])
        self.assertEqual(sealed["status"], "sealed")

    # ------------------------------------------------------------ 失效

    def test_object_update_invalidates_package_with_affected_items(self):
        pkg = self.store.create_basis_package("reviewer1", self.claim["id"], [self.ev["id"]], [self.evidence["id"]])
        self.store.seal_basis_package("reviewer1", pkg["id"])
        self.store.update_object("staff", self.obj["id"], {"public_summary": "补充了公开说明。"})
        sealed = self.store.get_basis_package("reviewer1", pkg["id"])
        self.assertEqual(sealed["status"], "invalid")
        self.assertFalse(sealed["is_valid"])
        types = {(a["type"], a["message"]) for a in sealed["affected_items"]}
        self.assertIn(("object", "藏品版本由 2 变为 3"), types)

    def test_event_add_invalidates_package(self):
        pkg = self.store.create_basis_package("reviewer1", self.claim["id"], [self.ev["id"]], [self.evidence["id"]])
        self.store.seal_basis_package("reviewer1", pkg["id"])
        new_ev = self.store.add_event("staff", self.obj["id"], "exhibition", "2001-05-01", "", "展厅", "公开展出", None, "public")
        sealed = self.store.get_basis_package("reviewer1", pkg["id"])
        self.assertEqual(sealed["status"], "invalid")
        self.assertTrue(any(a["type"] == "event" and a["id"] == new_ev["id"] for a in sealed["affected_items"]))

    def test_evidence_upload_invalidates_package(self):
        pkg = self.store.create_basis_package("reviewer1", self.claim["id"], [self.ev["id"]], [self.evidence["id"]])
        self.store.seal_basis_package("reviewer1", pkg["id"])
        new_ev = self.store.upload_evidence("staff", self.obj["id"], "extra.pdf", b64("extra material"), "internal", self.ev["id"])
        sealed = self.store.get_basis_package("reviewer1", pkg["id"])
        self.assertEqual(sealed["status"], "invalid")
        self.assertTrue(any(a["type"] == "evidence" and a["id"] == new_ev["id"] for a in sealed["affected_items"]))

    # ------------------------------------------------------------ 流转被挡与复审重试

    def test_transition_blocked_when_package_invalid_then_revalidate_and_retry(self):
        pkg = self.store.create_basis_package("reviewer1", self.claim["id"], [self.ev["id"]], [self.evidence["id"]])
        self.store.seal_basis_package("reviewer1", pkg["id"])
        # 藏品变化 → 依据包失效。
        self.store.update_object("staff", self.obj["id"], {"public_summary": "补充说明。"})
        # 未执行的流转被挡住，列出受影响项。
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", self.claim["id"], "under_review", "材料齐全，进入调查。")
        self.assertEqual(ctx.exception.code, "basis_package_invalid")
        self.assertTrue(any(a["type"] == "object" for a in ctx.exception.affected))
        # 主张状态未变。
        claim = self.store.get_object("reviewer1", self.obj["id"])["claims"][0]
        self.assertEqual(claim["status"], "submitted")
        # 失败任务保留未完成项。
        jobs = self.store.list_jobs("reviewer1", self.claim["id"])
        blocked = [j for j in jobs if j["status"] == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("basis_check", blocked[0]["error"]["unfinished"])
        # 复审：按当前藏品/事件/证据重新建包并封存。
        new_pkg = self.store.revalidate_basis_package("reviewer1", pkg["id"])
        self.assertEqual(new_pkg["status"], "draft")
        self.store.seal_basis_package("reviewer1", new_pkg["id"])
        # 重试：只续做未完成部分，流转成功。
        retried = self.store.retry_job("reviewer1", blocked[0]["id"])
        self.assertEqual(retried["status"], "completed")
        claim = self.store.get_object("reviewer1", self.obj["id"])["claims"][0]
        self.assertEqual(claim["status"], "under_review")

    def test_retry_is_idempotent_and_skips_completed_steps(self):
        pkg = self.store.create_basis_package("reviewer1", self.claim["id"], [self.ev["id"]], [self.evidence["id"]])
        self.store.seal_basis_package("reviewer1", pkg["id"])
        job = self.store.transition_claim("reviewer1", self.claim["id"], "under_review", "材料齐全，进入调查。")
        self.assertEqual(job["status"], "completed")
        # 对已完成任务重试，直接返回完成，不重复执行。
        again = self.store.retry_job("reviewer1", job["id"])
        self.assertEqual(again["status"], "completed")
        claim = self.store.get_object("reviewer1", self.obj["id"])["claims"][0]
        self.assertEqual(claim["status"], "under_review")
        self.assertEqual(len(claim["reviews"]), 1)

    # ------------------------------------------------------------ 持久化（重启后进度照旧）

    def test_progress_persists_across_restart(self):
        pkg = self.store.create_basis_package("reviewer1", self.claim["id"], [self.ev["id"]], [self.evidence["id"]])
        self.store.seal_basis_package("reviewer1", pkg["id"])
        self.store.update_object("staff", self.obj["id"], {"public_summary": "补充说明。"})
        with self.assertRaises(BusinessError):
            self.store.transition_claim("reviewer1", self.claim["id"], "under_review", "材料齐全，进入调查。")
        jobs_before = self.store.list_jobs("reviewer1", self.claim["id"])
        blocked_id = [j for j in jobs_before if j["status"] == "blocked"][0]["id"]
        # 模拟服务重启：新建存储实例指向同一数据库文件。
        restarted = ProvenanceStore(self.db)
        restarted.seed()
        job = restarted.get_job("reviewer1", blocked_id)
        self.assertEqual(job["status"], "blocked")
        self.assertIn("basis_check", job["error"]["unfinished"])
        # 重启后复审 + 重试，进度照旧可查、可续做。
        new_pkg = restarted.revalidate_basis_package("reviewer1", pkg["id"])
        restarted.seal_basis_package("reviewer1", new_pkg["id"])
        retried = restarted.retry_job("reviewer1", blocked_id)
        self.assertEqual(retried["status"], "completed")
        claim = restarted.get_object("reviewer1", self.obj["id"])["claims"][0]
        self.assertEqual(claim["status"], "under_review")

    # ------------------------------------------------------------ 无包时流转仍兼容

    def test_transition_without_package_still_works(self):
        job = self.store.transition_claim("reviewer1", self.claim["id"], "under_review", "材料齐全，进入调查。")
        self.assertEqual(job["status"], "completed")
        claim = self.store.get_object("reviewer1", self.obj["id"])["claims"][0]
        self.assertEqual(claim["status"], "under_review")


if __name__ == "__main__":
    unittest.main()
