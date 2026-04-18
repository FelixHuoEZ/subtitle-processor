import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.utils.file_utils import sanitize_filename


def test_sanitize_filename_preserves_extension_and_trims_long_titles():
    long_title = (
        "范冰冰陨落真相：一个超级女星的政治“钓性”｜冯小刚｜崔永元｜习近平｜王岐山｜"
        "中国娱乐圈｜李冰冰｜赵薇｜林心如｜范冰冰为什么被封杀｜范冰冰事件｜范冰冰和"
        "范丞丞的关系｜范冰冰现状"
    )
    sanitised = sanitize_filename(f"{long_title}.srt")

    assert sanitised.endswith(".srt")
    assert len(sanitised.encode("utf-8")) <= 200
    assert "..." in sanitised  # 被截断的情况下应包含省略号


def test_sanitize_filename_keeps_short_ascii_names():
    filename = "sample_output.srt"
    sanitised = sanitize_filename(filename)
    assert sanitised == filename
