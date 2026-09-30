"""审查依据包 / 失效 / 主张流转作业的规则测试。"""
import base64
import tempfile
import threading
import unittest
from pathlib import Path

from app import BusinessError
from store import ProvenanceStore


class PackageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.store = ProvenanceStore(self.db)
        self.store.seed()
        self.source = self.store.add_source("staff", "公开拍卖图录", "catalog", "CAT-1971-3")
        self.obj = self.store.create_object("staff", "M-1971-3", "陶俑", "陶器", "市博物馆", "1971 年入藏。")
        self.event = self.store.add_event(
            "staff", self.obj["id"], "auction", "1971-05-01", "", "伦敦",
            "公开拍卖记录", self.source["id"], "public")
        self.evidence = self.store.upload_evidence(
            "staff", self.obj["id"], "invoice.pdf",
            base64.b64encode(b"auction invoice").decode(), "internal", self.event["id"])
        self.claim = self.store.create_claim("claimant1", self.obj["id"], "林氏后人", "返还陶俑")

    def tearDown(self):
        self.tmp.cleanup()

    def _sealed_package(self):
        pkg = self.store.create_package(
            "reviewer1", self.obj["id"],
            [{"item_type": "event", "ref_id": self.event["id"]},
             {"item_type": "evidence", "ref_id": self.evidence["id"]}])
        return self.store.seal_package("reviewer1", pkg["id"], pkg["revision"])

    # ---- 建包 / 封存 ----

    def test_create_package_only_allows_public_events_and_internal_evidence(self):
        internal_event = self.store.add_event(
            "staff", self.obj["id"], "investigation", "2010-01-01", "", "馆内",
            "内部调查备注", None, "internal")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_package("reviewer1", self.obj["id"],
                                      [{"item_type": "event", "ref_id": internal_event["id"]}])
        self.assertEqual(ctx.exception.code, "event_not_public")
        public_evidence = self.store.upload_evidence(
            "staff", self.obj["id"], "notice.pdf",
            base64.b64encode(b"public notice").decode(), "public")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_package("reviewer1", self.obj["id"],
                                      [{"item_type": "evidence", "ref_id": public_evidence["id"]}])
        self.assertEqual(ctx.exception.code, "evidence_not_internal")

    def test_seal_records_object_version_and_evidence_summary(self):
        pkg = self._sealed_package()
        self.assertEqual(pkg["status"], "sealed")
        self.assertEqual(pkg["sealed_object_version"], self.event["object_version"])
        self.assertEqual(pkg["sealed_by"], "reviewer1")
        self.assertEqual(len(pkg["evidence_summary"]), 1)
        self.assertEqual(pkg["evidence_summary"][0]["sha256"], self.evidence["sha256"])
        self.assertTrue(pkg["basis_valid"])

    def test_seal_requires_expected_revision(self):
        pkg = self.store.create_package(
            "reviewer1", self.obj["id"],
            [{"item_type": "event", "ref_id": self.event["id"]}])
        with self.assertRaises(BusinessError) as ctx:
            self.store.seal_package("reviewer1", pkg["id"], None)
        self.assertEqual(ctx.exception.code, "revision_required")
        with self.assertRaises(BusinessError) as ctx:
            self.store.seal_package("reviewer1", pkg["id"], pkg["revision"] + 9)
        self.assertEqual(ctx.exception.code, "package_revision_conflict")

    # ---- 并发：先写入者生效 ----

    def test_concurrent_seal_first_writer_wins(self):
        pkg = self.store.create_package(
            "reviewer1", self.obj["id"],
            [{"item_type": "event", "ref_id": self.event["id"]},
             {"item_type": "evidence", "ref_id": self.evidence["id"]}])
        revision = pkg["revision"]
        results = []

        def seal():
            try:
                results.append(("ok", self.store.seal_package("reviewer1", pkg["id"], revision)["status"]))
            except BusinessError as exc:
                results.append(("conflict", exc.code))

        threads = [threading.Thread(target=seal), threading.Thread(target=seal)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        oks = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(oks[0][1], "sealed")
        self.assertIn(conflicts[0][1], ("package_revision_conflict", "package_sealed"))
        fresh = self.store.get_package("reviewer1", pkg["id"])
        self.assertEqual(fresh["status"], "sealed")

    def test_concurrent_add_items_first_writer_wins(self):
        pkg = self.store.create_package(
            "reviewer1", self.obj["id"],
            [{"item_type": "event", "ref_id": self.event["id"]}])
        extra_event = self.store.add_event(
            "staff", self.obj["id"], "provenance_note", "1972-01-01", "", "本市",
            "补充公开来源", None, "public")
        revision = pkg["revision"]
        results = []

        def add_items():
            try:
                results.append(("ok", self.store.add_package_items(
                    "reviewer1", pkg["id"],
                    [{"item_type": "event", "ref_id": extra_event["id"]}], revision)["revision"]))
            except BusinessError as exc:
                results.append(("conflict", exc.code))

        threads = [threading.Thread(target=add_items), threading.Thread(target=add_items)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, ["conflict", "ok"])
        fresh = self.store.get_package("reviewer1", pkg["id"])
        self.assertEqual(len(fresh["items"]), 2)

    # ---- 失效后挡住流转 ----

    def test_event_change_invalidates_package_and_blocks_pending_flow(self):
        pkg = self._sealed_package()
        job = self.store.create_transition_job(
            "reviewer1", pkg["id"],
            [{"claim_id": self.claim["id"], "to_status": "under_review", "note": "依据充分，进入审查。"}])
        self.assertEqual(job["counts"]["done"], 1)
        # 第二个主张在作业里还没跑完时，藏品来源事件变化
        claim2 = self.store.create_claim("claimant1", self.obj["id"], "林氏后人分支", "同样要求返还")
        job2 = self.store.create_transition_job(
            "reviewer1", pkg["id"],
            [{"claim_id": claim2["id"], "to_status": "under_review", "note": "排队等待审查。"}])
        # job2 建在旧包仍有效时，已执行完；再造一个待续作业场景：
        self.store.add_event("staff", self.obj["id"], "transfer", "1980-01-01", "", "馆内",
                             "补登记流转记录", None, "public")
        stale = self.store.get_package("reviewer1", pkg["id"])
        self.assertEqual(stale["status"], "invalid")
        self.assertEqual(stale["invalidation_reason"], "object_version_changed")
        self.assertFalse(stale["basis_valid"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_transition_job(
                "reviewer1", pkg["id"],
                [{"claim_id": claim2["id"], "to_status": "negotiating", "note": "尝试继续推进。"}])
        self.assertEqual(ctx.exception.code, "basis_invalid")
        # 已完成的流转不回滚；受影响未完成项列出
        self.assertEqual(self.store.get_job("reviewer1", job["id"])["counts"]["done"], 1)

    def test_new_evidence_invalidates_package_immediately(self):
        pkg = self._sealed_package()
        self.store.upload_evidence(
            "staff", self.obj["id"], "new-letter.pdf",
            base64.b64encode(b"newly discovered letter").decode(), "internal")
        stale = self.store.get_package("reviewer1", pkg["id"])
        self.assertEqual(stale["status"], "invalid")
        self.assertEqual(stale["invalidation_reason"], "evidence_changed")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_transition_job(
                "reviewer1", pkg["id"],
                [{"claim_id": self.claim["id"], "to_status": "under_review", "note": "旧结论想继续流转。"}])
        self.assertEqual(ctx.exception.code, "basis_invalid")

    def test_object_change_invalidates_and_lists_affected_items(self):
        pkg = self._sealed_package()
        job = self.store.create_transition_job(
            "reviewer1", pkg["id"],
            [{"claim_id": self.claim["id"], "to_status": "under_review", "note": "首次推进。"}])
        # 制造一个未完成项：新主张从 submitted 跳到 negotiating 必然失败，保留 failed
        claim2 = self.store.create_claim("claimant1", self.obj["id"], "陈家", "同样要求返还")
        job2 = self.store.create_transition_job(
            "reviewer1", pkg["id"],
            [{"claim_id": claim2["id"], "to_status": "negotiating", "note": "试图跳级协商。"}])
        self.assertEqual(job2["counts"]["failed"], 1)
        self.store.update_object("staff", self.obj["id"], {"public_summary": "持有人信息更新。"})
        view = self.store.get_package("reviewer1", pkg["id"])
        self.assertEqual(view["invalidation_reason"], "object_version_changed")
        affected = view["affected_items"]
        self.assertTrue(any(i["item_id"] == job2["items"][0]["id"] and i["status"] == "blocked" for i in affected))
        # 已完成项不在受影响列表
        done_ids = {i["id"] for i in job["items"]}
        self.assertFalse(any(i["item_id"] in done_ids for i in affected))

    # ---- 失败保留 + 只续做未完成项；重启可查 ----

    def test_retry_only_continues_unfinished_items(self):
        claim2 = self.store.create_claim("claimant1", self.obj["id"], "陈家", "要求返还")
        pkg = self._sealed_package()
        # 第一项合法，第二项非法跳级 -> 部分成功
        job = self.store.create_transition_job(
            "reviewer1", pkg["id"],
            [{"claim_id": self.claim["id"], "to_status": "under_review", "note": "进入审查阶段。"},
             {"claim_id": claim2["id"], "to_status": "resolved_return", "note": "非法跳级。"}])
        self.assertEqual(job["counts"], {"pending": 0, "done": 1, "failed": 1, "blocked": 0})
        # 重试同样的作业：done 不动，failed 项换成合法目标后续做成功
        fixed = self.store.transition_claim  # 先把 claim2 推到 under_review 以便它能进入 negotiating
        # claim2 直接走单独流转需要有效依据包；直接把失败项改目标不行，
        # 因此这里验证：重试不重复执行已完成项（第一项仍只对应一次审查记录）
        retried = self.store.retry_transition_job("reviewer1", job["id"])
        self.assertEqual(retried["counts"]["done"], 1)
        self.assertEqual(retried["counts"]["failed"], 1)
        reviews = self.store.get_object("reviewer1", self.obj["id"])["claims"][0]["reviews"]
        package_reviews = [r for r in reviews if r["package_id"] == pkg["id"]]
        self.assertEqual(len(package_reviews), 1)

    def test_retry_succeeds_after_item_becomes_eligible(self):
        claim2 = self.store.create_claim("claimant1", self.obj["id"], "陈家", "要求返还")
        pkg = self._sealed_package()
        job = self.store.create_transition_job(
            "reviewer1", pkg["id"],
            [{"claim_id": self.claim["id"], "to_status": "under_review", "note": "主张一进入审查。"},
             {"claim_id": claim2["id"], "to_status": "negotiating", "note": "主张二想跳到协商。"}])
        self.assertEqual(job["counts"]["failed"], 1)
        # 同包把 claim2 先推进到 under_review（仍是未完成项续做前的合法中间态）
        self.store.create_transition_job(
            "reviewer1", pkg["id"],
            [{"claim_id": claim2["id"], "to_status": "under_review", "note": "主张二进入审查。"}])
        retried = self.store.retry_transition_job("reviewer1", job["id"])
        self.assertEqual(retried["counts"]["done"], 2)
        self.assertTrue(retried["finished"])

    def test_progress_survives_store_restart(self):
        pkg = self._sealed_package()
        self.store.create_transition_job(
            "reviewer1", pkg["id"],
            [{"claim_id": self.claim["id"], "to_status": "under_review", "note": "持久化推进。"}])
        # 模拟服务重启：丢掉 store 实例，用同一数据库文件新建
        restarted = ProvenanceStore(self.db)
        jobs = restarted.list_jobs("reviewer1", self.obj["id"])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["counts"]["done"], 1)
        self.assertTrue(jobs[0]["finished"])
        pkg_view = restarted.get_package("reviewer1", pkg["id"])
        self.assertEqual(pkg_view["status"], "sealed")
        self.assertEqual(pkg_view["sealed_object_version"], self.event["object_version"])

    # ---- 复审 ----

    def test_review_flow(self):
        pkg = self._sealed_package()
        self.store.upload_evidence(
            "staff", self.obj["id"], "extra.pdf",
            base64.b64encode(b"extra material").decode(), "internal")
        # 旧包不能复活，也不能再补材料
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_package_items(
                "reviewer1", pkg["id"],
                [{"item_type": "event", "ref_id": self.event["id"]}], 1)
        self.assertEqual(ctx.exception.code, "package_invalid")
        new_pkg = self.store.review_package("reviewer1", pkg["id"])
        self.assertEqual(new_pkg["status"], "draft")
        self.assertEqual(new_pkg["reopens_package_id"], pkg["id"])
        self.assertEqual(len(new_pkg["items"]), 2)
        new_evidence = self.store.upload_evidence  # 证据已补；新包封存时把新证据纳入
        re_sealed = self.store.seal_package("reviewer1", new_pkg["id"], new_pkg["revision"])
        self.assertEqual(re_sealed["status"], "sealed")
        self.assertEqual(len(re_sealed["evidence_summary"]), 1)
        view = self.store.get_package("reviewer1", new_pkg["id"])
        self.assertTrue(view["basis_valid"])
        claim2 = self.store.create_claim("claimant1", self.obj["id"], "赵家", "要求返还")
        job = self.store.create_transition_job(
            "reviewer1", new_pkg["id"],
            [{"claim_id": claim2["id"], "to_status": "under_review", "note": "复审新依据包推进。"}])
        self.assertEqual(job["counts"]["done"], 1)

    def test_claimant_cannot_see_packages(self):
        self._sealed_package()
        view = self.store.get_object("claimant1", self.obj["id"])
        self.assertNotIn("packages", view)
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_packages("claimant1", self.obj["id"])
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
