import unittest
from unittest.mock import AsyncMock, patch

from app.services.knowledge_engines.chunk_enhancer import enhance_chunk, retrieval_text


class ChunkEnhancerTests(unittest.IsolatedAsyncioTestCase):
    async def test_filename_is_persisted_and_used_for_recall_without_llm(self):
        metadata = await enhance_chunk(
            "分段正文", "采购制度.pdf",
            {"include_filename": True, "auto_summary": False,
             "auto_questions": False, "image_caption": False}, {}, {})
        self.assertEqual(metadata["filename"], "采购制度.pdf")
        self.assertIn("文档名：采购制度.pdf", retrieval_text("分段正文", metadata))

    async def test_requested_generations_are_persisted_and_used_for_recall(self):
        generated = ('{"summary":"采购流程说明","questions":["如何发起采购？"],'
                     '"image_captions":[{"image":"flow.png","caption":"采购流程图"}]}')
        with patch('app.services.knowledge_engines.chunk_enhancer.llm_client.chat',
                   new=AsyncMock(return_value=generated)):
            metadata = await enhance_chunk(
                '查看流程 ![流程](images/flow.png)', '采购制度.pdf',
                {"include_filename": True, "auto_summary": True,
                 "auto_questions": True, "image_caption": True},
                {"flow.png": b"image bytes"},
                {"base_url": "https://llm.example/v1", "api_key": "test", "model": "vision-model"})
        recall = retrieval_text("查看流程", metadata)
        self.assertEqual(metadata["summary"], "采购流程说明")
        self.assertEqual(metadata["questions"], ["如何发起采购？"])
        self.assertEqual(metadata["image_captions"][0]["caption"], "采购流程图")
        self.assertIn("采购流程说明", recall)
        self.assertIn("如何发起采购？", recall)
        self.assertIn("采购流程图", recall)

    async def test_missing_llm_is_reported_without_losing_filename(self):
        metadata = await enhance_chunk(
            "文本", "制度.md",
            {"include_filename": True, "auto_summary": True,
             "auto_questions": False, "image_caption": False}, {}, {})
        self.assertEqual(metadata["filename"], "制度.md")
        self.assertEqual(metadata["summary"], "")
        self.assertIn("summary", metadata["errors"])

if __name__ == "__main__":
    unittest.main()
