import unittest

from clipforge.captions import ass_text, escape_ass, group_captions, srt_text
from clipforge.common import Span, Transcript, Word


def transcript(words):
    return Transcript(language="en", words=words,
                      spans=[Span(id="s0", start_s=125.0, end_s=130.0, text="Example.")],
                      duration_s=140.0)


class CaptionTests(unittest.TestCase):
    def test_source_word_126_becomes_point_six(self):
        source = transcript([Word(start_s=126.0, end_s=126.5, text="Hello.")])
        cue = group_captions(source, 125.4, 128.0)[0]
        self.assertAlmostEqual(cue.start_s, 0.6)
        self.assertIn("00:00:00,600 --> 00:00:01,100", srt_text([cue]))

    def test_intersection_clamps_both_edges_and_excludes_outside(self):
        source = transcript([
            Word(start_s=124.0, end_s=124.4, text="Outside."),
            Word(start_s=125.0, end_s=126.0, text="First."),
            Word(start_s=127.5, end_s=129.0, text="Last."),
            Word(start_s=129.1, end_s=130.0, text="Outside."),
        ])
        cues = group_captions(source, 125.4, 128.0)
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start_s, 0.0)
        self.assertAlmostEqual(cues[-1].end_s, 2.6)
        self.assertNotIn("Outside", srt_text(cues))

    def test_grouping_and_wrapping_preserve_spoken_words(self):
        words = [Word(start_s=125.0+i*0.3, end_s=125.25+i*0.3, text=t)
                 for i, t in enumerate("A useful explanation preserves every qualification and negation.".split())]
        cues = group_captions(transcript(words), 125.0, 130.0, line_width=20)
        flattened = " ".join(" ".join(c.text.split()) for c in cues)
        self.assertEqual(flattened, "A useful explanation preserves every qualification and negation.")
        self.assertTrue(all(len(line) <= 20 for c in cues for line in c.text.splitlines()))

    def test_ass_markup_and_new_records_cannot_be_injected(self):
        raw = r"{\p1}draw\Nhidden" + "\nDialogue: 0,evil"
        escaped = escape_ass(raw)
        self.assertNotIn("{", escaped)
        self.assertNotIn(r"\p1", escaped)
        self.assertNotIn("\n", escaped)
        source = transcript([Word(start_s=126.0, end_s=127.0, text=raw)])
        text = ass_text(group_captions(source, 125.4, 128.0))
        self.assertEqual(sum(line.startswith("Dialogue:") for line in text.splitlines()), 1)
        self.assertIn("PlayResX: 1080", text)
        self.assertIn("PlayResY: 1920", text)

    def test_srt_html_is_literal_and_font_cannot_inject_style(self):
        source = transcript([Word(start_s=126.0, end_s=127.0, text="<b>spoken</b>")])
        cues = group_captions(source, 125.4, 128.0)
        self.assertIn("&lt;b&gt;spoken&lt;/b&gt;", srt_text(cues))
        with self.assertRaises(ValueError):
            ass_text(cues, font="Arial\n[Events]")


if __name__ == "__main__":
    unittest.main()
