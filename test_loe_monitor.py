# -*- coding: utf-8 -*-
"""Офлайн-тести: python -m unittest test_loe_monitor -v"""
import json
import os
import re
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import requests

import loe_outage_monitor as m

# Тести не повинні залежати від змінних середовища розробника.
for _name in [k for k in os.environ if k.startswith("LOE_")]:
    del os.environ[_name]

SAMPLE_HTML = """
<p><b>Графік погодинних відключень на 18.10.2026</b></p>
<p>Інформація станом на 17.10.2026 14:30</p>
<p><b>Група 1.1.</b> Електроенергії немає з 08:00 до 12:00, з 18:00 до 22:00.</p>
<p><b>Група 1.2.</b> Електроенергії немає 10:00 - 14:00.</p>
<p><b>Група 2.1.</b> Електроенергія є.</p>
"""


class FakeApi:
    def __init__(self):
        self.items = []

    @property
    def item(self):                      # зворотна сумісність зі старим set_graph()
        return self.items[0] if self.items else {}

    @item.setter
    def item(self, value):
        self.items = [value]

    def fetch_menu_items(self):
        return self.items


class FakeNotifier:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.fail_urls = set()           # image_url (або None), для яких send() має падати

    def send(self, image_url, title, details):
        if self.fail or image_url in self.fail_urls:
            raise RuntimeError("telegram down")
        self.sent.append((image_url, title, details))


class ParserTests(unittest.TestCase):
    def test_basic_groups_and_ranges(self):
        g = m.parse_schedule_from_html(SAMPLE_HTML)
        self.assertEqual(g, {"1.1": [["08:00", "12:00"], ["18:00", "22:00"]],
                             "1.2": [["10:00", "14:00"]]})

    def test_inline_groups_in_one_line(self):
        g = m.parse_schedule_from_html("Група 1.1: 08:00-12:00; Група 1.2: 10:00–14:00; Група 2.1: 00:00 - 04:30")
        self.assertEqual(g["1.1"], [["08:00", "12:00"]])
        self.assertEqual(g["1.2"], [["10:00", "14:00"]])
        self.assertEqual(g["2.1"], [["00:00", "04:30"]])

    def test_group_list_and_continuation_lines(self):
        g = m.parse_schedule_from_html("<p>Групи 1.1, 1.2:</p><p>з 00:00 до 04:30</p><p>з 12:00 до 16:00</p>")
        self.assertEqual(g["1.1"], [["00:00", "04:30"], ["12:00", "16:00"]])
        self.assertEqual(g["1.2"], g["1.1"])

    def test_time_first_layout(self):
        g = m.parse_schedule_from_html("<p>08:00 - 12:00 — групи 3.1, 3.2</p>")
        self.assertEqual(set(g), {"3.1", "3.2"})

    def test_midnight_and_overnight(self):
        g = m.parse_schedule_from_html("Група 1.1: 22:00 - 00:00; Група 1.2: 23:00 - 02:00")
        self.assertEqual(g["1.1"], [["22:00", "24:00"]])
        self.assertEqual(g["1.2"], [["00:00", "02:00"], ["23:00", "24:00"]])

    def test_adjacent_ranges_merge(self):
        g = m.parse_schedule_from_html("Група 1.1: 00:00-04:30, 04:30-08:00")
        self.assertEqual(g["1.1"], [["00:00", "08:00"]])

    def test_dates_not_parsed_as_groups(self):
        self.assertEqual(m.parse_schedule_from_html("на 18.10.2026 станом на 17.10.2026 14:30"), {})

    def test_nbsp_and_entities(self):
        g = m.parse_schedule_from_html("<p>Група&nbsp;1.1:&nbsp;08:00&nbsp;-&nbsp;12:00</p>")
        self.assertEqual(g, {"1.1": [["08:00", "12:00"]]})

    def test_bare_group_fallback(self):
        g = m.parse_schedule_from_html("<p>1.1 — 08:00-12:00</p><p>1.2 — 10:00-14:00</p>")
        self.assertEqual(set(g), {"1.1", "1.2"})

    # ---- скидання контексту груп ----
    def test_no_outage_group_does_not_leak_into_info_lines(self):
        html = ("<p>Група 1.1. Електроенергії немає 08:00 - 12:00.</p>"
                "<p>Група 2.1. Електроенергія є.</p>"
                "<p>Інформація станом на 14:00 - 15:00</p>"
                "<p>Гаряча лінія: 08:00 - 20:00</p>")
        self.assertEqual(m.parse_schedule_from_html(html), {"1.1": [["08:00", "12:00"]]})

    def test_no_outage_markers_do_not_keep_context(self):
        for phrase in ("Електроенергія є.", "Відключень немає.", "Не вимикається.", "Без відключень.",
                       "Світло є."):
            with self.subTest(phrase=phrase):
                html = f"<p>Група 2.1. {phrase}</p><p>з 08:00 до 20:00</p>"
                self.assertEqual(m.parse_schedule_from_html(html), {})

    def test_outage_phrase_is_not_no_outage_marker(self):
        g = m.parse_schedule_from_html("<p>Група 1.1. Електроенергії немає</p><p>з 08:00 до 12:00</p>")
        self.assertEqual(g, {"1.1": [["08:00", "12:00"]]})

    def test_info_line_resets_context(self):
        html = "<p>Група 1.1:</p><p>Станом на 14:00 - 15:00</p><p>з 08:00 до 12:00</p>"
        self.assertEqual(m.parse_schedule_from_html(html), {})

    def test_hotline_line_resets_context(self):
        html = "<p>Група 1.1. 08:00 - 12:00</p><p>Гаряча лінія: 08:00 - 20:00</p>"
        self.assertEqual(m.parse_schedule_from_html(html), {"1.1": [["08:00", "12:00"]]})

    def test_line_without_groups_and_times_resets_context(self):
        html = "<p>Група 1.1:</p><p>Просимо зберігати спокій.</p><p>з 08:00 до 12:00</p>"
        self.assertEqual(m.parse_schedule_from_html(html), {})

    def test_continuation_still_works_across_time_only_lines(self):
        html = "<p>Групи 1.1, 1.2:</p><p>з 00:00 до 04:30</p><p>з 12:00 до 16:00</p>"
        g = m.parse_schedule_from_html(html)
        self.assertEqual(g["1.2"], [["00:00", "04:30"], ["12:00", "16:00"]])

    def test_info_prefix_not_taken_as_time_first_range(self):
        g = m.parse_schedule_from_html("<p>Станом на 14:00 - 15:00 Група 2.1. Електроенергія є.</p>")
        self.assertEqual(g, {})

    def test_inline_no_outage_group_between_outage_groups(self):
        g = m.parse_schedule_from_html("Група 1.1: 08:00-12:00; Група 1.2: електроенергія є; Група 2.1: 10:00-14:00")
        self.assertEqual(g, {"1.1": [["08:00", "12:00"]], "2.1": [["10:00", "14:00"]]})

    # ---- часові інтервали ----
    def test_dot_separator_for_00_and_30_minutes(self):
        g = m.parse_schedule_from_html("Група 1.1: з 08.00 до 12.30, 14.30 - 18.00")
        self.assertEqual(g["1.1"], [["08:00", "12:30"], ["14:30", "18:00"]])

    def test_dot_separator_does_not_eat_dates_or_groups(self):
        self.assertEqual(m.extract_ranges("17.10 - 18.10"), [])
        self.assertEqual(m.extract_ranges("з 17.10.2026 по 18.10.2026"), [])
        self.assertEqual(m.extract_ranges("групи 1.1 - 1.2"), [])
        self.assertEqual(m.extract_ranges("з 08.15 до 12.45"), [])       # не :00/:30 → не час
        self.assertEqual(m.extract_ranges("08.00 - 12.00.2026"), [])
        self.assertEqual(m.parse_schedule_from_html("Група 1.1 - 1.2"), {})

    def test_mixed_colon_and_dot(self):
        self.assertEqual(m.extract_ranges("08:00 - 12.30"), [(480, 750)])

    def test_whole_day_ranges(self):
        self.assertEqual(m.extract_ranges("00:00 - 24:00"), [(0, 1440)])
        self.assertEqual(m.extract_ranges("з 00:00 до 00:00"), [(0, 1440)])
        self.assertEqual(m.extract_ranges("з 00.00 до 24.00"), [(0, 1440)])
        g = m.parse_schedule_from_html("Група 3.1: з 00:00 до 00:00")
        self.assertEqual(g, {"3.1": [["00:00", "24:00"]]})

    def test_degenerate_ranges_ignored(self):
        self.assertEqual(m.extract_ranges("12:00 - 12:00"), [])
        self.assertEqual(m.extract_ranges("24:00 - 02:00"), [])
        self.assertEqual(m.extract_ranges("08:00 - 25:00"), [])


class DateTests(unittest.TestCase):
    TODAY = date(2026, 10, 17)

    def test_prefers_schedule_date_over_published_date(self):
        text = m.html_to_text(SAMPLE_HTML)
        self.assertEqual(m.extract_schedule_date(text, self.TODAY), date(2026, 10, 18))

    def test_prefers_schedule_date_even_if_info_line_first(self):
        text = "Станом на 17.10.2026 14:30\nГрафік на 18.10.2026"
        self.assertEqual(m.extract_schedule_date(text, self.TODAY), date(2026, 10, 18))

    def test_info_phrase_before_schedule_phrase_in_same_sentence(self):
        text = "Станом на 14:00 графік на 18.10.2026"
        self.assertEqual(m._date_priority(text.lower(), text.lower().index("18.10")), 0)
        self.assertEqual(m.extract_schedule_date(text, self.TODAY), date(2026, 10, 18))

    def test_info_date_in_same_sentence_loses(self):
        text = "Станом на 14:00 графік на 18.10.2026, оновлено 17.10.2026"
        self.assertEqual(m.extract_schedule_date(text, self.TODAY), date(2026, 10, 18))
        t = text.lower()
        self.assertEqual(m._date_priority(t, t.index("17.10")), 2)

    def test_weekday_between_na_and_date(self):
        for text in ("Графік на неділю, 18.10.2026", "графік на неділю (18.10.2026)",
                     "Графік на завтра, 18.10.2026", "Графік на п'ятницю 18.10.2026",
                     "Графік на нд, 18.10.2026"):
            with self.subTest(text=text):
                t = text.lower()
                self.assertEqual(m._date_priority(t, t.index("18.10")), 0)

    def test_weekday_with_info_date_first(self):
        text = "Станом на 17.10.2026 14:30. Графік на неділю, 18.10.2026"
        self.assertEqual(m.extract_schedule_date(text, self.TODAY), date(2026, 10, 18))

    def test_verbal_date_with_weekday(self):
        text = "Станом на 14:00 графік на неділю, 18 жовтня"
        self.assertEqual(m.extract_schedule_date(text, self.TODAY), date(2026, 10, 18))

    def test_stanom_na_date_is_lowest_priority(self):
        for text in ("станом на 17.10.2026", "інформація оновлена на 17.10.2026",
                     "актуально на 17.10.2026", "Оновлено: 17.10.2026", "на момент 17.10.2026"):
            with self.subTest(text=text):
                t = text.lower()
                self.assertEqual(m._date_priority(t, t.index("17.10")), 2)

    def test_plain_date_is_neutral(self):
        t = "дата 18.10.2026"
        self.assertEqual(m._date_priority(t, t.index("18.10")), 1)

    def test_na_as_word_suffix_is_not_marker(self):
        t = "україна 18.10.2026"
        self.assertEqual(m._date_priority(t, t.index("18.10")), 1)

    def test_previous_sentence_marker_does_not_leak(self):
        t = "станом на 17.10.2026. нова дата 18.10.2026"
        self.assertEqual(m._date_priority(t, t.index("18.10")), 1)

    def test_verbal_date(self):
        self.assertEqual(m.extract_schedule_date("Графік на 18 жовтня 2026 року", self.TODAY),
                         date(2026, 10, 18))

    def test_verbal_date_without_year(self):
        self.assertEqual(m.extract_schedule_date("графік на 5 січня", date(2026, 12, 30)),
                         date(2027, 1, 5))

    def test_none(self):
        self.assertIsNone(m.extract_schedule_date("немає дати", self.TODAY))

    def test_date_from_url(self):
        self.assertEqual(m.extract_date_from_url("https://x/media/2026-10-18_GPV.png"), date(2026, 10, 18))
        self.assertEqual(m.extract_date_from_url("https://x/media/18.10.2026_GPV.png"), date(2026, 10, 18))


class IntervalTests(unittest.TestCase):
    def test_subtract(self):
        self.assertEqual(m.subtract_intervals([(600, 840)], [(600, 720)]), [(720, 840)])
        self.assertEqual(m.subtract_intervals([(600, 840)], [(660, 700)]), [(600, 660), (700, 840)])
        self.assertEqual(m.subtract_intervals([(600, 840)], []), [(600, 840)])
        self.assertEqual(m.subtract_intervals([(600, 840)], [(0, 1440)]), [])


class DiffTests(unittest.TestCase):
    OLD = {"1.1": [["08:00", "12:00"]], "1.2": [["10:00", "14:00"]], "3.1": [["10:00", "12:00"]]}
    NEW = {"1.1": [["08:00", "12:00"], ["14:00", "16:00"]], "1.2": [["10:00", "13:00"]],
           "4.2": [["20:00", "22:00"]]}

    def test_diff(self):
        d = m.diff_schedules(self.OLD, self.NEW)
        self.assertEqual(d.added_hours, {"1.1": [(840, 960)]})
        self.assertEqual(d.removed_hours, {"1.2": [(780, 840)]})
        self.assertEqual(d.added_groups, {"4.2": [(1200, 1320)]})
        self.assertEqual(d.removed_groups, {"3.1": [(600, 720)]})

    def test_empty_diff(self):
        self.assertTrue(m.diff_schedules(self.OLD, self.OLD).is_empty())


class TemplateTests(unittest.TestCase):
    D = date(2026, 10, 18)

    def test_scenario1_first(self):
        g = m.parse_schedule_from_html(SAMPLE_HTML)
        title, details = m.build_notification(m.KIND_NEW, self.D, g, None)
        self.assertEqual(title, "<b>НОВИЙ ГРАФІК ВІДКЛЮЧЕННЯ СВІТЛА на 18.10.2026</b>")
        self.assertIn("• Група 1.1: 08:00 - 12:00, 18:00 - 22:00", details)

    def test_scenario1_diff(self):
        d = m.diff_schedules(DiffTests.OLD, DiffTests.NEW)
        _, details = m.build_notification(m.KIND_NEW, self.D, DiffTests.NEW, d)
        self.assertIn("📊 <b>Зміни в графіку:</b>", details)
        self.assertIn("➕ <b>Додано години:</b>", details)
        self.assertIn("• Група 1.1: 14:00 - 16:00", details)
        self.assertIn("➖ <b>Скасовано відключення (додано світло):</b>", details)
        self.assertIn("👥 <b>Зміни по групах:</b>", details)
        self.assertIn("• Групу 4.2 додано до графіків", details)

    def test_scenario2(self):
        self.assertEqual(m.build_notification(m.KIND_TOMORROW_FIRST, self.D, {}, None),
                         ("<b>ГРАФІК ВІДКЛЮЧЕННЯ СВІТЛА НА ЗАВТРА</b>", ""))

    def test_scenario3(self):
        d = m.diff_schedules(DiffTests.OLD, DiffTests.NEW)
        title, details = m.build_notification(m.KIND_TOMORROW_UPDATE, self.D, DiffTests.NEW, d)
        self.assertEqual(title, "<b>ОНОВЛЕНИЙ ГРАФІК НА ЗАВТРА</b>")
        for part in ("📊 <b>Що змінилося:</b>", "➕ <b>Додано години відключень:</b>",
                     "➖ <b>Забрано години (буде світло):</b>", "🔄 <b>Зміни в групах:</b>"):
            self.assertIn(part, details)

    def test_scenario3_image_only(self):
        d = m.diff_schedules(DiffTests.OLD, DiffTests.OLD)
        _, details = m.build_notification(m.KIND_TOMORROW_UPDATE, self.D, DiffTests.OLD, d)
        self.assertIn(m.NO_DIFF_NOTE, details)

    def test_split_and_visible_len(self):
        self.assertEqual(m.visible_len("<b>абв</b> &amp;"), 5)
        text = "\n".join(f"line {i}" for i in range(2000))
        chunks = m.split_message(text, 500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("\n".join(chunks), text)


    def test_split_message_overlong_line_prefers_spaces(self):
        words = " ".join(f"слово{i}" for i in range(1500))          # один рядок ≫ ліміту
        text = "short\n" + words + "\ntail"
        chunks = m.split_message(text, 500)
        self.assertTrue(all(0 < len(c) <= 500 for c in chunks))
        self.assertEqual(chunks[0], "short")
        self.assertTrue(chunks[-1].endswith("\ntail"))               # залишок + наступний рядок
        # розрізи по пробілах: жодне слово не розірване, нічого не втрачено
        rebuilt = " ".join(chunks[1:]).split()
        self.assertEqual(rebuilt, words.split() + ["tail"])

    def test_split_message_hard_cut_without_spaces(self):
        text = "x" * 1234
        chunks = m.split_message(text, 500)
        self.assertEqual([len(c) for c in chunks], [500, 500, 234])
        self.assertEqual("".join(chunks), text)

    def test_split_message_keeps_html_tags_and_entities_intact(self):
        text = "<b>" + "&amp;" * 300 + "</b> " + "<a href=\"http://x/y z\">link</a>" * 50
        for limit in (30, 50, 99, 100, 333):   # ліміт ≥ найдовшого тега (23)
            chunks = m.split_message(text, limit)
            self.assertTrue(all(len(c) <= limit for c in chunks), limit)
            for c in chunks:
                self.assertEqual(c.count("<"), c.count(">"), (limit, c))
                self.assertNotRegex(c.replace("&amp;", ""), r"&")

    def test_split_message_invariant_for_arbitrary_input(self):
        import random
        rnd = random.Random(42)
        alphabet = "ab \n<>&;/\"=xyz"
        for _ in range(300):
            text = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 400)))
            limit = rnd.randint(1, 60)
            chunks = m.split_message(text, limit)
            self.assertTrue(all(len(c) <= limit for c in chunks), (text, limit))

    def test_split_message_rejects_bad_limit(self):
        with self.assertRaises(ValueError):
            m.split_message("abc", 0)


class ScenarioFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api, self.notifier = FakeApi(), FakeNotifier()
        self.now = datetime(2026, 10, 17, 14, 0, tzinfo=m.KYIV_TZ)
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.mon = m.OutageMonitor(self.api, self.notifier, m.StateStore(self.state_path),
                                   clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def set_graph(self, day, groups_html, img="/media/a_GPV.png"):
        self.api.item = {"imageUrl": img,
                         "rawHtml": f"<p>Графік на {day}</p><p>станом на 17.10.2026 13:00</p>{groups_html}"}

    def test_today_first_then_unchanged_then_changed(self):
        self.set_graph("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>")
        self.assertEqual(self.mon.check_once(), "sent")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("на 17.10.2026", self.notifier.sent[0][1])
        self.assertIn("Заплановані відключення", self.notifier.sent[0][2])
        self.assertEqual(self.notifier.sent[0][0], "https://api.loe.lviv.ua/media/a_GPV.png")

        self.assertEqual(self.mon.check_once(), "unchanged")
        self.assertEqual(len(self.notifier.sent), 1)

        self.set_graph("17.10.2026", "<p>Група 1.1: 08:00-12:00, 18:00-20:00</p><p>Група 2.2: 10:00-11:00</p>", "/media/b_GPV.png")
        self.assertEqual(self.mon.check_once(), "sent")
        _, title, details = self.notifier.sent[1]
        self.assertIn("НОВИЙ ГРАФІК", title)
        self.assertIn("• Група 1.1: 18:00 - 20:00", details)
        self.assertIn("• Групу 2.2 додано до графіків", details)

    def test_tomorrow_first_then_update(self):
        self.set_graph("18.10.2026", "<p>Група 1.1: 08:00-12:00</p>")
        self.mon.check_once()
        self.assertEqual(self.notifier.sent[0][1:], ("<b>ГРАФІК ВІДКЛЮЧЕННЯ СВІТЛА НА ЗАВТРА</b>", ""))
        self.set_graph("18.10.2026", "<p>Група 1.1: 09:00-12:00</p>", "/media/c_GPV.png")
        self.mon.check_once()
        self.assertIn("ОНОВЛЕНИЙ ГРАФІК НА ЗАВТРА", self.notifier.sent[1][1])
        self.assertIn("Забрано години", self.notifier.sent[1][2])

    def test_tomorrow_becomes_today_without_spam(self):
        self.set_graph("18.10.2026", "<p>Група 1.1: 08:00-12:00</p>")
        self.mon.check_once()
        self.now += timedelta(days=1)          # настала північ, API віддає той самий графік
        self.assertEqual(self.mon.check_once(), "unchanged")
        self.assertEqual(len(self.notifier.sent), 1)

    def test_stale_schedule_is_silent(self):
        self.set_graph("15.10.2026", "<p>Група 1.1: 08:00-12:00</p>")
        self.assertEqual(self.mon.check_once(), "stale")
        self.assertEqual(self.notifier.sent, [])

    def test_state_not_saved_when_send_fails(self):
        self.set_graph("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>")
        self.notifier.fail = True
        with self.assertRaises(RuntimeError):
            self.mon.check_once()
        self.notifier.fail = False
        self.assertEqual(self.mon.check_once(), "sent")   # повтор у наступному циклі

    def test_state_persisted_with_expected_shape(self):
        self.set_graph("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>")
        self.mon.check_once()
        mon2 = m.OutageMonitor(self.api, self.notifier, m.StateStore(self.state_path),
                               clock=lambda: self.now)
        entry = mon2.state.data["2026-10-17"]
        self.assertEqual(set(entry) >= {"image_url", "hash", "parsed_groups", "is_first_broadcast"}, True)
        self.assertEqual(entry["parsed_groups"], {"1.1": [["08:00", "12:00"]]})
        self.assertEqual(mon2.check_once(), "unchanged")

    def test_prune_old_entries(self):
        self.mon.state.data["2026-09-01"] = {}
        self.set_graph("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>")
        self.mon.check_once()
        self.assertNotIn("2026-09-01", self.mon.state.data)


    # ---- кілька елементів menuItems (сьогодні + завтра) ----
    @staticmethod
    def make_item(day, groups_html, img="/media/a_GPV.png"):
        return {"imageUrl": img,
                "rawHtml": f"<p>Графік на {day}</p><p>станом на 17.10.2026 13:00</p>{groups_html}"}

    def test_today_and_tomorrow_in_one_cycle(self):
        self.api.items = [
            self.make_item("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>", "/media/t_GPV.png"),
            self.make_item("18.10.2026", "<p>Група 1.1: 10:00-14:00</p>", "/media/n_GPV.png"),
        ]
        self.assertEqual(self.mon.check_once(), "sent")
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("на 17.10.2026", self.notifier.sent[0][1])
        self.assertEqual(self.notifier.sent[1][1], "<b>ГРАФІК ВІДКЛЮЧЕННЯ СВІТЛА НА ЗАВТРА</b>")
        self.assertEqual({"2026-10-17", "2026-10-18"}, set(self.mon.state.data))

        self.assertEqual(self.mon.check_once(), "unchanged")      # жодного спаму
        self.assertEqual(len(self.notifier.sent), 2)

        # змінився лише завтрашній графік → одне сповіщення
        self.api.items[1] = self.make_item("18.10.2026", "<p>Група 1.1: 11:00-14:00</p>",
                                           "/media/n2_GPV.png")
        self.assertEqual(self.mon.check_once(), "sent")
        self.assertEqual(len(self.notifier.sent), 3)
        self.assertIn("ОНОВЛЕНИЙ ГРАФІК НА ЗАВТРА", self.notifier.sent[2][1])

    def test_items_processed_in_date_order(self):
        self.api.items = [
            self.make_item("18.10.2026", "<p>Група 1.1: 10:00-14:00</p>", "/media/n_GPV.png"),
            self.make_item("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>", "/media/t_GPV.png"),
        ]
        self.mon.check_once()
        self.assertTrue(self.notifier.sent[0][0].endswith("t_GPV.png"))

    def test_failed_item_does_not_block_others(self):
        self.api.items = [
            self.make_item("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>", "/media/t_GPV.png"),
            self.make_item("18.10.2026", "<p>Група 1.1: 10:00-14:00</p>", "/media/n_GPV.png"),
        ]
        self.notifier.fail_urls = {"https://api.loe.lviv.ua/media/t_GPV.png"}
        with self.assertRaises(RuntimeError):
            self.mon.check_once()
        self.assertEqual(len(self.notifier.sent), 1)               # завтрашній таки пішов
        self.assertEqual(set(self.mon.state.data), {"2026-10-18"})  # сьогоднішній НЕ збережено

        self.notifier.fail_urls = set()
        self.assertEqual(self.mon.check_once(), "sent")            # повтор лише для сьогоднішнього
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertIn("на 17.10.2026", self.notifier.sent[1][1])

    def test_empty_items_are_skipped(self):
        self.api.items = [{"imageUrl": "", "rawHtml": ""}, {},
                          self.make_item("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>")]
        self.assertEqual(self.mon.check_once(), "sent")
        self.assertEqual(len(self.notifier.sent), 1)

    def test_all_items_empty_is_not_an_error(self):
        self.api.items = [{"imageUrl": "", "rawHtml": ""}, {"imageUrl": "/media/banner.png"}]
        self.assertEqual(self.mon.check_once(), "unchanged")
        self.assertEqual(self.notifier.sent, [])

    def test_duplicate_dates_do_not_flap(self):
        self.api.items = [
            self.make_item("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>", "/media/a_GPV.png"),
            self.make_item("17.10.2026", "<p>Група 1.1: 09:00-12:00</p>", "/media/b_GPV.png"),
        ]
        self.assertEqual(self.mon.check_once(), "sent")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.mon.check_once(), "unchanged")       # без «пінг-понгу» між елементами
        self.assertEqual(len(self.notifier.sent), 1)

    # ---- скасування графіка (немає `_GPV` / порожній imageUrl) ----
    def test_cancelled_graph_without_image_sent_as_text(self):
        self.api.items = [{"imageUrl": "", "rawHtml": "<p>Графік на 17.10.2026 скасовано</p>"}]
        self.assertEqual(self.mon.check_once(), "sent")
        image_url, title, details = self.notifier.sent[0]
        self.assertIsNone(image_url)
        self.assertIn("на 17.10.2026", title)
        self.assertIn("скасовано", details)
        self.assertEqual(self.mon.check_once(), "unchanged")

    def test_non_gpv_image_is_treated_as_text_only(self):
        self.api.items = [{"imageUrl": "/media/banner.png",
                           "rawHtml": "<p>Графік на 17.10.2026 скасовано</p>"}]
        self.mon.check_once()
        self.assertIsNone(self.notifier.sent[0][0])

    def test_fallback_text_is_html_escaped(self):
        self.api.items = [{"imageUrl": None,
                           "rawHtml": "<p>Графік на 17.10.2026 &lt;скасовано&gt; &amp; тест</p>"}]
        self.mon.check_once()
        self.assertIn("&lt;скасовано&gt; &amp; тест", self.notifier.sent[0][2])

    def test_cancellation_text_change_is_detected(self):
        self.api.items = [{"imageUrl": "", "rawHtml": "<p>Графік на 17.10.2026 скасовано</p>"}]
        self.mon.check_once()
        self.api.items = [{"imageUrl": "", "rawHtml": "<p>Графік на 17.10.2026 скасовано до вечора</p>"}]
        self.assertEqual(self.mon.check_once(), "sent")
        self.assertEqual(len(self.notifier.sent), 2)

    def test_cancel_after_published_graph_reports_removed_groups(self):
        self.set_graph("17.10.2026", "<p>Група 1.1: 08:00-12:00</p>")
        self.mon.check_once()
        self.api.items = [{"imageUrl": "", "rawHtml": "<p>Графік на 17.10.2026 скасовано</p>"}]
        self.assertEqual(self.mon.check_once(), "sent")
        image_url, _, details = self.notifier.sent[1]
        self.assertIsNone(image_url)
        self.assertIn("• Групу 1.1 прибрано з графіків", details)
        self.assertIn("скасовано", details)

    def test_old_hash_format_is_preserved_when_groups_found(self):
        g = {"1.1": [["08:00", "12:00"]]}
        self.assertEqual(m.compute_hash("u", g), m.compute_hash("u", g, ""))
        self.assertNotEqual(m.compute_hash("u", {}), m.compute_hash("u", {}, "текст"))


class ApiShapeTests(unittest.TestCase):
    def test_extract_menu_item(self):
        data = {"hydra:member": [{"menuItems": [{"imageUrl": "/x.png", "rawHtml": "<p/>"}]}]}
        self.assertEqual(m.extract_menu_item(data)["imageUrl"], "/x.png")
        with self.assertRaises(ValueError):
            m.extract_menu_item({"hydra:member": []})

    def test_extract_menu_items_returns_all(self):
        data = {"hydra:member": [{"menuItems": [{"imageUrl": "/a_GPV.png"}, "junk",
                                                {"imageUrl": "/b_GPV.png"}]}]}
        items = m.extract_menu_items(data)
        self.assertEqual([it["imageUrl"] for it in items], ["/a_GPV.png", "/b_GPV.png"])
        self.assertEqual(m.extract_menu_item(data)["imageUrl"], "/a_GPV.png")   # обгортка
        for bad in ({"hydra:member": []}, {"hydra:member": [{"menuItems": []}]},
                    {"hydra:member": [{"menuItems": ["x"]}]}):
            with self.assertRaises(ValueError):
                m.extract_menu_items(bad)

    def test_is_gpv_image(self):
        self.assertTrue(m.is_gpv_image("/media/2026-10-17_GPV.png"))
        self.assertTrue(m.is_gpv_image("/media/x_gpv.png"))
        for v in (None, "", "/media/banner.png"):
            self.assertFalse(m.is_gpv_image(v))

    def test_absolute_url(self):
        self.assertEqual(m.absolute_media_url("/media/a.png"), "https://api.loe.lviv.ua/media/a.png")
        self.assertEqual(m.absolute_media_url("https://cdn/x.png"), "https://cdn/x.png")


class NotifierTests(unittest.TestCase):
    class Tg:
        def __init__(self, photo_error=False, message_error=None, all_down=False):
            self.calls, self.photo_error = [], photo_error
            self.message_error, self.all_down = message_error, all_down

        def _maybe_down(self):
            if self.all_down:
                raise requests.ConnectionError("network down")

        def send_photo(self, image, filename, caption):
            self.calls.append(("photo", caption))
            self._maybe_down()
            if isinstance(self.photo_error, BaseException):     # напр. requests.Timeout
                raise self.photo_error
            if self.photo_error:
                raise m.TelegramError("sendPhoto", 400, "PHOTO_INVALID_DIMENSIONS")

        def send_document(self, image, filename, caption):
            self.calls.append(("document", caption))
            self._maybe_down()

        def send_photo_url(self, url, caption):
            self.calls.append(("photo_url", caption))
            self._maybe_down()

        def send_message(self, text):
            self.calls.append(("message", text))
            self._maybe_down()
            if self.message_error:
                raise self.message_error

    class Api:
        def __init__(self, ok=True):
            self.ok = ok

        def download_image(self, url):
            if not self.ok:
                raise ValueError("no image")
            from io import BytesIO
            from PIL import Image
            buf = BytesIO()
            Image.new("RGB", (100, 100)).save(buf, "PNG")
            return buf.getvalue()

    def test_short_caption_single_photo(self):
        tg = self.Tg()
        m.TelegramNotifier(tg, self.Api()).send("https://x/a_GPV.png", "<b>T</b>", "details")
        self.assertEqual(tg.calls, [("photo", "<b>T</b>\n\ndetails")])

    def test_long_caption_goes_to_follow_up_message(self):
        tg = self.Tg()
        details = "\n".join(f"• Група 1.1: {i}" for i in range(200))
        m.TelegramNotifier(tg, self.Api()).send("https://x/a_GPV.png", "<b>T</b>", details)
        self.assertEqual(tg.calls[0], ("photo", "<b>T</b>"))
        self.assertTrue(all(c[0] == "message" for c in tg.calls[1:]))
        self.assertEqual("\n".join(c[1] for c in tg.calls[1:]), details)

    def test_photo_error_falls_back_to_document(self):
        tg = self.Tg(photo_error=True)
        m.TelegramNotifier(tg, self.Api()).send("https://x/a_GPV.png", "<b>T</b>", "")
        self.assertEqual([c[0] for c in tg.calls], ["photo", "document"])

    def test_download_failure_falls_back_to_url(self):
        tg = self.Tg()
        m.TelegramNotifier(tg, self.Api(ok=False)).send("https://x/a_GPV.png", "<b>T</b>", "")
        self.assertEqual([c[0] for c in tg.calls], ["photo_url"])

    # ---- мережеві фолбеки ----
    def test_photo_timeout_falls_back_to_document(self):
        tg = self.Tg(photo_error=requests.Timeout("upload timed out"))
        m.TelegramNotifier(tg, self.Api()).send("https://x/a_GPV.png", "<b>T</b>", "")
        self.assertEqual([c[0] for c in tg.calls], ["photo", "document"])

    def test_network_down_raises_requests_exception_after_all_attempts(self):
        tg = self.Tg(all_down=True)
        with self.assertRaises(requests.RequestException):
            m.TelegramNotifier(tg, self.Api()).send("https://x/a_GPV.png", "<b>T</b>", "")
        self.assertEqual([c[0] for c in tg.calls], ["photo", "document", "photo_url", "message"])

    # ---- текстовий режим (image_url=None / без _GPV) ----
    def test_no_image_sends_text_only(self):
        tg = self.Tg()
        m.TelegramNotifier(tg, self.Api()).send(None, "<b>T</b>", "details")
        self.assertEqual(tg.calls, [("message", "<b>T</b>\n\ndetails")])

    def test_non_gpv_url_sends_text_only(self):
        tg = self.Tg()
        m.TelegramNotifier(tg, self.Api()).send("https://x/banner.png", "<b>T</b>", "")
        self.assertEqual(tg.calls, [("message", "<b>T</b>")])

    def test_text_only_long_message_is_chunked(self):
        tg = self.Tg()
        details = "\n".join(f"• Група 1.1: {i}" for i in range(1000))
        m.TelegramNotifier(tg, self.Api()).send(None, "<b>T</b>", details)
        self.assertGreater(len(tg.calls), 1)
        self.assertTrue(all(c[0] == "message" and len(c[1]) <= m.MESSAGE_LIMIT for c in tg.calls))
        self.assertEqual("\n".join(c[1] for c in tg.calls), f"<b>T</b>\n\n{details}")

    # ---- втрата даних: невдала відправка деталей має піднімати помилку ----
    def test_text_only_failure_raises(self):
        tg = self.Tg(message_error=requests.ConnectionError("down"))
        with mock.patch.object(m.time, "sleep"):
            with self.assertRaises(m.TelegramError):
                m.TelegramNotifier(tg, self.Api()).send(None, "<b>T</b>", "details")
        self.assertEqual(len(tg.calls), 3)                      # 3 спроби

    def test_follow_up_chunk_failure_raises(self):
        tg = self.Tg(message_error=m.TelegramError("sendMessage", 500, "boom"))
        details = "\n".join(f"• Група 1.1: {i}" for i in range(200))   # довгий підпис
        with mock.patch.object(m.time, "sleep"):
            with self.assertRaises(m.TelegramError):
                m.TelegramNotifier(tg, self.Api()).send("https://x/a_GPV.png", "<b>T</b>", details)
        self.assertEqual(tg.calls[0][0], "photo")               # фото пішло, деталі — ні

    def test_failed_follow_up_keeps_state_unsaved_in_monitor(self):
        tg = self.Tg(message_error=m.TelegramError("sendMessage", 500, "boom"))
        api = FakeApi()
        api.items = [{"imageUrl": "/media/a_GPV.png",
                      "rawHtml": "<p>Графік на 17.10.2026</p><p>Група 1.1: 08:00-12:00</p>"}]
        now = datetime(2026, 10, 17, 14, 0, tzinfo=m.KYIV_TZ)
        with tempfile.TemporaryDirectory() as d:
            mon = m.OutageMonitor(api, m.TelegramNotifier(tg, self.Api()),
                                  m.StateStore(os.path.join(d, "s.json")), clock=lambda: now)
            # примусово робимо підпис «задовгим», щоб деталі йшли окремим повідомленням
            with mock.patch.object(m, "CAPTION_LIMIT", 10), mock.patch.object(m.time, "sleep"):
                with self.assertRaises(m.TelegramError):
                    mon.check_once()
            self.assertEqual(mon.state.data, {})                # хеш НЕ збережено

class MiscTests(unittest.TestCase):
    def test_fallback_timezone(self):
        tz = m._FallbackKyiv()
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz)
        summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz)
        self.assertEqual(winter.hour, 14)
        self.assertEqual(summer.hour, 15)
        # перехід на зимовий час 2026-10-25 о 01:00 UTC
        self.assertEqual(datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).astimezone(tz).hour, 3)
        self.assertEqual(datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).astimezone(tz).hour, 3)

    def test_fallback_timezone_utcoffset_around_transitions(self):
        tz = m._FallbackKyiv()
        two, three = timedelta(hours=2), timedelta(hours=3)

        def local(y, mo, d, h, mi):
            return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).astimezone(tz)

        # осінь: 2026-10-25 о 01:00 UTC
        before = local(2026, 10, 25, 0, 59)
        self.assertEqual((before.hour, before.minute, before.fold, before.utcoffset()), (3, 59, 0, three))
        first_after = local(2026, 10, 25, 1, 0)
        self.assertEqual((first_after.hour, first_after.minute, first_after.fold), (3, 0, 1))
        self.assertEqual(first_after.utcoffset(), two)
        mid = local(2026, 10, 25, 1, 30)
        self.assertEqual((mid.hour, mid.minute, mid.fold, mid.utcoffset()), (3, 30, 1, two))
        self.assertEqual(mid.tzname(), "EET")
        after = local(2026, 10, 25, 2, 0)
        self.assertEqual((after.hour, after.fold, after.utcoffset()), (4, 0, two))
        # весна: 2026-03-29 о 01:00 UTC
        self.assertEqual(local(2026, 3, 29, 0, 59).utcoffset(), two)
        spring = local(2026, 3, 29, 1, 0)
        self.assertEqual((spring.hour, spring.utcoffset(), spring.tzname()), (4, three, "EEST"))

    def test_fallback_timezone_respects_fold_for_wall_clock(self):
        tz = m._FallbackKyiv()
        two, three = timedelta(hours=2), timedelta(hours=3)
        amb = datetime(2026, 10, 25, 3, 30)
        self.assertEqual(amb.replace(tzinfo=tz, fold=0).utcoffset(), three)
        self.assertEqual(amb.replace(tzinfo=tz, fold=1).utcoffset(), two)
        # неоднозначність не впливає на «звичайні» дати
        self.assertEqual(datetime(2026, 10, 25, 4, 0, tzinfo=tz).utcoffset(), two)
        self.assertEqual(datetime(2026, 10, 25, 2, 59, tzinfo=tz).utcoffset(), three)
        # утиліта --> UTC і назад
        for h, mi in ((0, 30), (0, 59), (1, 0), (1, 30), (2, 0)):
            t = datetime(2026, 10, 25, h, mi, tzinfo=timezone.utc)
            self.assertEqual(t.astimezone(tz).astimezone(timezone.utc), t)

    def test_fallback_timezone_matches_zoneinfo(self):
        try:
            from zoneinfo import ZoneInfo
            ref = ZoneInfo("Europe/Kyiv")
        except Exception:
            self.skipTest("немає zoneinfo/tzdata")
        tz = m._FallbackKyiv()
        t = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2028, 1, 1, tzinfo=timezone.utc)
        while t < end:
            a, b = t.astimezone(ref), t.astimezone(tz)
            self.assertEqual((a.utcoffset(), a.fold), (b.utcoffset(), b.fold), t)
            t += timedelta(minutes=30)

    def test_resource_path_falls_back_to_app_dir_not_cwd(self):
        with tempfile.TemporaryDirectory() as other:
            old_cwd = os.getcwd()
            os.chdir(other)                      # імітуємо cwd = C:\\Windows\\System32
            try:
                if hasattr(m.sys, "_MEIPASS"):
                    self.skipTest("запущено всередині PyInstaller")
                self.assertEqual(m.resource_path("icon.ico"),
                                 os.path.join(m.app_dir(), "icon.ico"))
            finally:
                os.chdir(old_cwd)

    def test_resource_path_prefers_meipass(self):
        with mock.patch.object(m.sys, "_MEIPASS", "/tmp/_MEI123", create=True):
            self.assertEqual(m.resource_path("icon.ico"), os.path.join("/tmp/_MEI123", "icon.ico"))

    def test_single_instance_sets_ctypes_signature(self):
        import ctypes
        kernel32 = mock.MagicMock()
        kernel32.CreateMutexW.return_value = 12345
        with mock.patch.object(m.os, "name", "nt"), \
                mock.patch.object(m.ctypes, "WinDLL", create=True, return_value=kernel32), \
                mock.patch.object(m.ctypes, "get_last_error", create=True, return_value=0), \
                mock.patch.object(m, "_mutex_handle", None):
            self.assertTrue(m.acquire_single_instance())
        self.assertEqual(kernel32.CreateMutexW.argtypes,
                         [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p])
        with mock.patch.object(m.os, "name", "nt"), \
                mock.patch.object(m.ctypes, "WinDLL", create=True, return_value=kernel32), \
                mock.patch.object(m.ctypes, "get_last_error", create=True, return_value=183), \
                mock.patch.object(m, "_mutex_handle", None):
            self.assertFalse(m.acquire_single_instance())

    def test_icon_fallback_and_ico_file(self):
        img = m.make_fallback_icon(64)
        self.assertEqual(img.size, (64, 64))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "icon.ico")
            m.make_icon_file(p)
            self.assertGreater(os.path.getsize(p), 100)

    def test_config_template_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with self.assertRaises(m.ConfigError):
                m.load_config(p)                      # створює шаблон
            self.assertTrue(os.path.isfile(p))
            with self.assertRaises(m.ConfigError):
                m.load_config(p)                      # токени ще не заповнені
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"BOT_TOKEN":"123456:ABC-def","CHAT_ID":-100123,"CHECK_INTERVAL_SECONDS":5}')
            with self.assertRaises(m.ConfigError):
                m.load_config(p)                      # без ADMIN_ID — помилка
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"BOT_TOKEN":"123456:ABC-def","ADMIN_ID":42,"CHAT_ID":-100123,'
                        '"CHECK_INTERVAL_SECONDS":5}')
            cfg = m.load_config(p)
            self.assertEqual((cfg.chat_id, cfg.interval), ("-100123", m.MIN_INTERVAL_SECONDS))

    def test_token_redaction(self):
        import logging
        m.RedactingFormatter.secrets[:] = ["123:SECRET"]
        rec = logging.LogRecord("x", logging.ERROR, "", 0, "url /bot123:SECRET/sendPhoto", None, None)
        self.assertNotIn("SECRET", m.RedactingFormatter("%(message)s").format(rec))
        m.RedactingFormatter.secrets[:] = []


# ══════════════════════════════════════════════════════════════════════════════
# Адмін-панель, доступ, групи, розсилка, швидкість
# ══════════════════════════════════════════════════════════════════════════════
ADMIN = 7_000_000_001      # вигаданий ID адміністратора лише для тестів


class FakeTg:
    """Підробний TelegramClient: записує все, що «надіслано», і вміє імітувати збої."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sent, self.edits, self.answers, self.left = [], [], [], []
        self.media = []                 # (chat_id, вид, підпис/текст) — сповіщення про графіки
        self.failures = {}              # chat_id → список винятків (споживається по одному)
        self.always = {}                # chat_id → виняток на кожну відправку

    def _check(self, chat_id):
        chat_id = str(chat_id)
        if chat_id in self.always:
            raise self.always[chat_id]
        queue = self.failures.get(chat_id)
        if queue:
            raise queue.pop(0)

    # керування
    def send_message(self, text, chat_id=None, reply_markup=None):
        self._check(chat_id)
        with self.lock:
            self.sent.append((str(chat_id), text, reply_markup))

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        with self.lock:
            self.edits.append((str(chat_id), message_id, text, reply_markup))

    def answer_callback(self, callback_id, text="", alert=False):
        with self.lock:
            self.answers.append((callback_id, text, alert))

    def leave_chat(self, chat_id):
        with self.lock:
            self.left.append(str(chat_id))

    # розсилка сповіщень
    def bind(self, chat_id):
        return FakeBound(self, str(chat_id))

    def texts_to(self, chat_id):
        return [t for c, t, _ in self.sent if c == str(chat_id)]


class FakeBound:
    def __init__(self, tg, chat_id):
        self.tg, self.chat_id = tg, chat_id

    def _rec(self, kind, text):
        self.tg._check(self.chat_id)
        with self.tg.lock:
            self.tg.media.append((self.chat_id, kind, text))

    def send_photo(self, image, filename, caption):
        self._rec("photo", caption)

    def send_document(self, image, filename, caption):
        self._rec("document", caption)

    def send_photo_url(self, url, caption):
        self._rec("photo_url", caption)

    def send_message(self, text):
        self._rec("message", text)


class CountingApi:
    def __init__(self):
        self.downloads = 0

    def download_image(self, url):
        self.downloads += 1
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", (100, 100)).save(buf, "PNG")
        return buf.getvalue()


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "access.json")


class ConfigAdminTests(TmpDirCase):
    TOKEN = "1:abc"

    def write(self, **cfg):
        p = os.path.join(self.tmp.name, "config.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return p

    def read(self, p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def load(self, **cfg):
        cfg.setdefault("BOT_TOKEN", self.TOKEN)
        cfg.setdefault("ADMIN_ID", 42)
        return m.load_config(self.write(**cfg))

    def test_template_requires_token_and_admin(self):
        p = os.path.join(self.tmp.name, "config.json")
        with self.assertRaises(m.ConfigError):
            m.load_config(p)
        raw = self.read(p)
        self.assertIn("PUT_YOUR", raw["BOT_TOKEN"])
        self.assertIn("PUT_YOUR", raw["ADMIN_ID"])          # жодного чужого ID за замовчуванням
        self.assertEqual(raw["CHECK_INTERVAL_SECONDS"], 60)
        self.assertEqual(raw["CONFIG_VERSION"], m.CONFIG_VERSION)

    def test_no_hardcoded_admin_anywhere(self):
        self.assertFalse(hasattr(m, "ADMIN_ID_DEFAULT"))
        self.assertFalse(any(isinstance(v, int) and v > 1000 for v in m.CONFIG_TEMPLATE.values()))
        with open(m.__file__, encoding="utf-8") as f:
            source = f.read()
        long_numbers = re.findall(r"(?<![\w.])\d{9,}(?![\w.])", source)
        # «123456789» — очевидний приклад у підказках команд, не реальний ID.
        self.assertEqual([n for n in long_numbers if n != "123456789"], [])

    def test_admin_id_is_required(self):
        with self.assertRaises(m.ConfigError) as ctx:
            m.load_config(self.write(BOT_TOKEN=self.TOKEN))
        self.assertIn("ADMIN_ID", str(ctx.exception))

    def test_bad_admin_id_is_rejected(self):
        for bad in ("abc", -5, 0, None, "PUT_YOUR_TELEGRAM_ID_HERE"):
            with self.subTest(bad=bad), self.assertRaises(m.ConfigError):
                self.load(ADMIN_ID=bad)

    def test_admin_id_accepts_int_and_string(self):
        for value in (42, "42", " 42 "):
            with self.subTest(value=value):
                self.assertEqual(self.load(ADMIN_ID=value).admin_id, 42)

    def test_chat_id_is_optional(self):
        cfg = self.load()
        self.assertEqual((cfg.chat_id, cfg.admin_id), ("", 42))

    def test_placeholder_chat_id_is_ignored(self):
        self.assertEqual(self.load(CHAT_ID="PUT_YOUR_CHAT_ID_HERE").chat_id, "")

    def test_legacy_600_migrates_once(self):
        p = self.write(BOT_TOKEN=self.TOKEN, CHAT_ID="-1001", ADMIN_ID=42, CHECK_INTERVAL_SECONDS=600)
        self.assertEqual(m.load_config(p).interval, 60)
        raw = self.read(p)
        self.assertEqual((raw["CHECK_INTERVAL_SECONDS"], raw["CONFIG_VERSION"]), (60, m.CONFIG_VERSION))
        self.assertEqual((raw["BOT_TOKEN"], raw["CHAT_ID"], raw["ADMIN_ID"]), (self.TOKEN, "-1001", 42))
        raw["CHECK_INTERVAL_SECONDS"] = 600           # свідомий вибір користувача після міграції
        with open(p, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        self.assertEqual(m.load_config(p).interval, 600)

    def test_custom_interval_survives_migration_and_is_clamped(self):
        self.assertEqual(self.load(CHECK_INTERVAL_SECONDS=120).interval, 120)
        self.assertEqual(self.load(CHECK_INTERVAL_SECONDS=5).interval, m.MIN_INTERVAL_SECONDS)

    def test_default_is_much_faster_than_before(self):
        self.assertEqual(m.DEFAULT_INTERVAL_SECONDS, 60)
        self.assertLess(m.MIN_INTERVAL_SECONDS, 60)

    # ---- налаштування, раніше зашиті в коді ----
    def test_defaults_for_urls_and_retention(self):
        cfg = self.load()
        self.assertEqual((cfg.api_url, cfg.site_url, cfg.user_agent, cfg.state_retention_days),
                         (m.DEFAULT_API_URL, m.DEFAULT_SITE_URL, m.DEFAULT_USER_AGENT,
                          m.DEFAULT_STATE_RETENTION_DAYS))
        self.assertEqual(cfg.media_base, "https://api.loe.lviv.ua")

    def test_urls_user_agent_and_retention_are_configurable(self):
        cfg = self.load(API_URL="https://api.example.org:8443/v1/menus?x=1",
                        SITE_URL="https://example.org/power/", USER_AGENT="my-agent/1.0",
                        STATE_RETENTION_DAYS=14)
        self.assertEqual((cfg.api_url, cfg.site_url, cfg.user_agent, cfg.state_retention_days),
                         ("https://api.example.org:8443/v1/menus?x=1", "https://example.org/power/",
                          "my-agent/1.0", 14))
        self.assertEqual(cfg.media_base, "https://api.example.org:8443")

    def test_non_http_urls_are_rejected(self):
        for key in ("API_URL", "SITE_URL"):
            with self.subTest(key=key), self.assertRaises(m.ConfigError):
                self.load(**{key: "ftp://example.org"})

    def test_retention_is_at_least_one_day(self):
        self.assertEqual(self.load(STATE_RETENTION_DAYS=0).state_retention_days, 1)

    def test_env_overrides_file_and_works_without_file(self):
        env = {"LOE_BOT_TOKEN": "9:envtoken", "LOE_ADMIN_ID": "77", "LOE_CHAT_ID": "-100500",
               "LOE_CHECK_INTERVAL_SECONDS": "120"}
        with mock.patch.dict(os.environ, env):
            absent = os.path.join(self.tmp.name, "absent.json")
            cfg = m.load_config(absent)
            self.assertEqual((cfg.bot_token, cfg.admin_id, cfg.chat_id, cfg.interval),
                             ("9:envtoken", 77, "-100500", 120))
            self.assertFalse(os.path.exists(absent))     # файл не потрібен, шаблон не створюється
            cfg = self.load(BOT_TOKEN="1:file", ADMIN_ID=42)
            self.assertEqual((cfg.bot_token, cfg.admin_id), ("9:envtoken", 77))

    def test_env_secrets_are_not_written_back_to_config_file(self):
        p = self.write(BOT_TOKEN="1:file", ADMIN_ID=42, CHECK_INTERVAL_SECONDS=600)  # міграція перезапише файл
        with mock.patch.dict(os.environ, {"LOE_BOT_TOKEN": "9:envtoken", "LOE_ADMIN_ID": "77"}):
            m.load_config(p)
        raw = self.read(p)
        self.assertEqual((raw["BOT_TOKEN"], raw["ADMIN_ID"]), ("1:file", 42))

    def test_data_dir_from_env(self):
        target = os.path.join(self.tmp.name, "data", "nested")
        with mock.patch.dict(os.environ, {"LOE_MONITOR_DIR": target}):
            self.assertEqual(m.data_dir(), os.path.abspath(target))
        self.assertTrue(os.path.isdir(target))
        with mock.patch.dict(os.environ, {"LOE_MONITOR_DIR": ""}):
            self.assertEqual(m.data_dir(), m.app_dir())


class ConfigurableEndpointsTests(TmpDirCase):
    def test_absolute_media_url_with_custom_base(self):
        base = "https://api.example.org/"
        self.assertEqual(m.absolute_media_url("/media/a.png", base), "https://api.example.org/media/a.png")
        self.assertEqual(m.absolute_media_url("media/a.png", base), "https://api.example.org/media/a.png")
        self.assertEqual(m.absolute_media_url("https://cdn.example.org/a.png", base),
                         "https://cdn.example.org/a.png")

    def test_monitor_uses_configured_media_base(self):
        api, notifier = FakeApi(), FakeNotifier()
        api.item = {"imageUrl": "/media/a_GPV.png",
                    "rawHtml": "<p>Графік на 17.10.2026</p><p>Група 1.1: 08:00-12:00</p>"}
        mon = m.OutageMonitor(api, notifier, m.StateStore(os.path.join(self.tmp.name, "s.json")),
                              clock=lambda: datetime(2026, 10, 17, 14, 0, tzinfo=m.KYIV_TZ),
                              media_base="https://api.example.org")
        self.assertEqual(mon.check_once(), "sent")
        self.assertEqual(notifier.sent[0][0], "https://api.example.org/media/a_GPV.png")

    def test_api_client_uses_configured_urls(self):
        c = m.LoeApiClient("https://x.example/api/menus", "https://x.example/site/", "ua/1")
        self.assertEqual(c.session.headers["User-Agent"], "ua/1")
        self.assertEqual(c.session.headers["Referer"], "https://x.example/site/")
        self.assertEqual(c.session.headers["Origin"], "https://x.example")
        resp = mock.Mock()
        resp.json.return_value = {"hydra:member": [{"menuItems": [{"imageUrl": "x"}]}]}
        with mock.patch.object(c.session, "get", return_value=resp) as get:
            c.fetch_menu_items()
        self.assertEqual(get.call_args[0][0], "https://x.example/api/menus")

    def test_state_retention_is_configurable(self):
        store = m.StateStore(os.path.join(self.tmp.name, "s.json"), retention_days=2)
        store.data = {"2026-10-10": {}, "2026-10-16": {}, "2026-10-17": {}}
        store.prune(date(2026, 10, 17))
        self.assertEqual(sorted(store.data), ["2026-10-16", "2026-10-17"])


class RegistryTests(TmpDirCase):
    def reg(self, main_chat=""):
        return m.AccessRegistry(self.path, ADMIN, main_chat)

    def test_request_flow(self):
        r = self.reg()
        self.assertEqual(r.request_access(10, "Іван", "ivan"), "new")
        self.assertEqual(r.request_access(10, "Іван", "ivan"), "pending")
        self.assertEqual(r.request_access(ADMIN, "Адмін", ""), "allowed")
        self.assertFalse(r.is_allowed(10))

    def test_add_user_consumes_pending_and_keeps_profile(self):
        r = self.reg()
        r.request_access(10, "Іван", "ivan")
        r.add_user(10, by=ADMIN)
        self.assertTrue(r.is_allowed(10))
        self.assertEqual(r.counts(), {"users": 1, "pending": 0, "groups": 0})
        self.assertEqual((r.data["users"]["10"]["name"], r.data["users"]["10"]["username"]), ("Іван", "ivan"))

    def test_deny_blocks_repeat_requests_until_admin_adds(self):
        r = self.reg()
        r.request_access(10, "Іван", "")
        self.assertIsNotNone(r.deny(10))
        self.assertIsNone(r.deny(10))
        self.assertEqual(r.request_access(10, "Іван", ""), "denied")
        r.add_user(10)
        self.assertTrue(r.is_allowed(10))
        self.assertNotIn("10", r.data["denied"])

    def test_recipients_order_dedupe_and_flags(self):
        r = self.reg(main_chat="-1001")
        r.add_user(5)
        r.add_user(6)
        r.add_group(-200, "G", "supergroup", ADMIN)
        self.assertEqual(r.recipients(), [str(ADMIN), "-1001", "5", "6", "-200"])
        r.mark_user_inactive(5)
        r.set_admin_notify(False)
        self.assertEqual(r.recipients(), ["-1001", "6", "-200"])
        # основний чат = адмін: без дублів, навіть коли «мої сповіщення» вимкнено (зберігся 5 — неактивний)
        self.assertEqual(self.reg(main_chat=str(ADMIN)).recipients(), [str(ADMIN), "6", "-200"])

    def test_persistence_roundtrip(self):
        r = self.reg()
        r.add_user(5, "Оля", "olya")
        r.add_group(-200, "Група", "group", ADMIN)
        r.set_admin_notify(False)
        r2 = self.reg()
        self.assertTrue(r2.is_allowed(5))
        self.assertTrue(r2.has_group(-200))
        self.assertFalse(r2.admin_notify)

    def test_corrupt_file_is_backed_up(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        r = self.reg()
        self.assertEqual(r.counts(), {"users": 0, "pending": 0, "groups": 0})
        self.assertTrue(os.path.isfile(self.path + ".bak"))

    def test_touch_user_reactivates_and_updates_name(self):
        r = self.reg()
        r.add_user(5, "Стара", "")
        r.mark_user_inactive(5)
        self.assertTrue(r.touch_user(5, "Нова", "nova"))
        self.assertNotIn("inactive", r.data["users"]["5"])
        self.assertEqual(r.data["users"]["5"]["name"], "Нова")
        self.assertFalse(r.touch_user(999))

    def test_group_migrate_and_main_chat_migrate(self):
        r = self.reg(main_chat="-100")
        r.add_group(-100, "G", "group", ADMIN)
        self.assertEqual(r.migrate_chat(-100, -1000), "-1000")
        self.assertTrue(r.has_group(-1000))
        self.assertFalse(r.has_group(-100))
        self.assertEqual(r.main_chat_id, "-1000")

    def test_drop_recipient_kinds(self):
        r = self.reg()
        r.add_user(5)
        r.add_group(-200, "G", "group", ADMIN)
        self.assertEqual(r.drop_recipient(-200), "group")
        self.assertEqual(r.drop_recipient(5), "user")
        self.assertTrue(r.data["users"]["5"]["inactive"])
        self.assertEqual(r.drop_recipient(ADMIN), "other")
        self.assertTrue(r.is_allowed(5))              # користувач лишається в списку


class BroadcastTests(TmpDirCase):
    URL = "https://x/a_GPV.png"

    def setUp(self):
        super().setUp()
        self.tg, self.api = FakeTg(), CountingApi()
        self.reg = m.AccessRegistry(self.path, ADMIN)
        self.reg.add_user(5)
        self.reg.add_group(-200, "Група", "group", ADMIN)
        self.bc = m.BroadcastNotifier(self.tg, self.api, self.reg)

    def delivered(self):
        return sorted(c for c, _, _ in self.tg.media)

    def test_everyone_gets_it_and_image_is_downloaded_once(self):
        self.bc.send(self.URL, "<b>T</b>", "details")
        self.assertEqual(self.delivered(), sorted([str(ADMIN), "5", "-200"]))
        self.assertEqual(self.api.downloads, 1)
        self.assertTrue(all(kind == "photo" for _, kind, _ in self.tg.media))

    def test_admin_is_served_first(self):
        self.bc.send(self.URL, "<b>T</b>", "")
        self.assertEqual(self.tg.media[0][0], str(ADMIN))

    def test_text_only_broadcast(self):
        self.bc.send(None, "<b>T</b>", "d")
        self.assertEqual(self.api.downloads, 0)
        self.assertTrue(all(kind == "message" for _, kind, _ in self.tg.media))

    def test_transient_failure_retries_only_failed_recipient(self):
        self.tg.failures["5"] = [requests.ConnectionError("blip")] * 4     # photo, document, url, message
        with mock.patch.object(m.time, "sleep"):
            with self.assertRaises(requests.RequestException):
                self.bc.send(self.URL, "<b>T</b>", "d")
        self.assertEqual(sorted(c for c, _, _ in self.tg.media), sorted([str(ADMIN), "-200"]))
        self.tg.media.clear()
        self.tg.failures.clear()
        self.bc.send(self.URL, "<b>T</b>", "d")                              # наступний цикл
        self.assertEqual(self.delivered(), ["5"])                           # без дублів решті

    def test_new_payload_is_not_confused_with_pending_one(self):
        self.tg.always["5"] = requests.ConnectionError("down")
        with mock.patch.object(m.time, "sleep"):
            with self.assertRaises(requests.RequestException):
                self.bc.send(self.URL, "<b>A</b>", "")
            self.tg.media.clear()
            with self.assertRaises(requests.RequestException):
                self.bc.send(self.URL, "<b>B</b>", "")                       # інший текст
        self.assertIn(str(ADMIN), self.delivered())                          # B дійшов усім іншим

    def test_blocked_user_is_marked_inactive_and_does_not_block_others(self):
        self.tg.always["5"] = m.TelegramError("sendPhoto", 403, "Forbidden: bot was blocked by the user")
        self.bc.send(self.URL, "<b>T</b>", "d")                              # без винятку
        self.assertEqual(self.delivered(), sorted([str(ADMIN), "-200"]))
        self.assertTrue(self.reg.data["users"]["5"]["inactive"])
        self.assertTrue(any("5" in t for t in self.tg.texts_to(ADMIN)))      # адміна повідомлено
        self.assertNotIn("5", self.reg.recipients())

    def test_kicked_group_is_removed(self):
        self.tg.always["-200"] = m.TelegramError("sendPhoto", 403, "Forbidden: bot was kicked")
        self.bc.send(self.URL, "<b>T</b>", "")
        self.assertFalse(self.reg.has_group(-200))

    def test_chat_not_found_is_permanent(self):
        self.tg.always["-200"] = m.TelegramError("sendPhoto", 400, "Bad Request: chat not found")
        self.bc.send(self.URL, "<b>T</b>", "")
        self.assertFalse(self.reg.has_group(-200))

    def test_group_upgraded_to_supergroup_is_followed(self):
        # Telegram повторює цю помилку на кожну відправку у старий chat_id
        self.tg.always["-200"] = m.TelegramError("sendPhoto", 400, "group chat was upgraded",
                                                 migrate_to=-1000)
        self.bc.send(self.URL, "<b>T</b>", "")
        self.assertTrue(self.reg.has_group(-1000))
        self.assertFalse(self.reg.has_group(-200))
        self.assertIn("-1000", self.delivered())
        self.tg.media.clear()
        self.bc.send(self.URL, "<b>T</b>", "next")
        self.assertIn("-1000", self.delivered())                             # далі шлемо вже на новий id
        self.assertNotIn("-200", self.delivered())

    def test_gives_up_after_max_attempts(self):
        self.tg.always["5"] = requests.ConnectionError("down")
        with mock.patch.object(m.time, "sleep"):
            for _ in range(m.MAX_RECIPIENT_ATTEMPTS - 1):
                with self.assertRaises(requests.RequestException):
                    self.bc.send(self.URL, "<b>T</b>", "")
            self.bc.send(self.URL, "<b>T</b>", "")                           # остання спроба — без винятку

    def test_no_recipients_is_not_an_error(self):
        reg = m.AccessRegistry(os.path.join(self.tmp.name, "empty.json"), ADMIN)
        reg.set_admin_notify(False)
        m.BroadcastNotifier(self.tg, self.api, reg).send(self.URL, "<b>T</b>", "")
        self.assertEqual(self.tg.media, [])

    def test_monitor_integration_state_saved_only_after_all_delivered(self):
        api = FakeApi()
        api.items = [{"imageUrl": "/media/a_GPV.png",
                      "rawHtml": "<p>Графік на 17.10.2026</p><p>Група 1.1: 08:00-12:00</p>"}]
        now = datetime(2026, 10, 17, 14, 0, tzinfo=m.KYIV_TZ)
        state = m.StateStore(os.path.join(self.tmp.name, "s.json"))
        mon = m.OutageMonitor(api, self.bc, state, clock=lambda: now)
        self.tg.failures["5"] = [requests.ConnectionError("blip")] * 4
        with mock.patch.object(m.time, "sleep"):
            with self.assertRaises(requests.RequestException):
                mon.check_once()
        self.assertEqual(state.data, {})
        self.tg.media.clear()
        self.assertEqual(mon.check_once(), "sent")
        self.assertEqual(self.delivered(), ["5"])
        self.assertEqual(mon.check_once(), "unchanged")


class TelegramClientTests(unittest.TestCase):
    def test_second_chat_reuses_file_id_instead_of_uploading(self):
        c = m.TelegramClient("1:abc", "100")
        calls = []

        def fake_call(method, data, files=None, **kw):
            calls.append((method, dict(data), files is not None))
            if files:
                return {"ok": True, "result": {"photo": [{"file_id": "small"}, {"file_id": "BIG"}]}}
            return {"ok": True, "result": {}}

        with mock.patch.object(c, "_call", side_effect=fake_call):
            c.send_photo(b"img", "a.png", "cap", chat_id=1)
            c.send_photo(b"img", "a.png", "cap", chat_id=2)
            c.bind(3).send_photo(b"img", "a.png", "cap")
            c.send_photo(b"other", "b.png", "cap", chat_id=4)
        self.assertEqual([x[2] for x in calls], [True, False, False, True])
        self.assertEqual(calls[1][1]["photo"], "BIG")
        self.assertEqual((calls[1][1]["chat_id"], calls[2][1]["chat_id"]), ("2", "3"))

    def test_stale_file_id_falls_back_to_upload(self):
        c = m.TelegramClient("1:abc", "100")
        c._file_ids[("photo", __import__("hashlib").sha1(b"img").hexdigest())] = "OLD"
        calls = []

        def fake_call(method, data, files=None, **kw):
            calls.append(files is not None)
            if not files:
                raise m.TelegramError(method, 400, "Bad Request: wrong file identifier")
            return {"ok": True, "result": {"photo": [{"file_id": "NEW"}]}}

        with mock.patch.object(c, "_call", side_effect=fake_call):
            c.send_photo(b"img", "a.png", "cap", chat_id=1)
        self.assertEqual(calls, [False, True])
        self.assertIn("NEW", c._file_ids.values())

    def test_default_chat_id_still_used_without_binding(self):
        c = m.TelegramClient("1:abc", "-100500")
        with mock.patch.object(c, "_call", return_value={"ok": True}) as call:
            c.send_message("hi")
        self.assertEqual(call.call_args[0][1]["chat_id"], "-100500")

    def test_error_exposes_migration_target(self):
        c = m.TelegramClient("1:abc", "1")
        resp = mock.Mock(status_code=400, text="x")
        resp.json.return_value = {"ok": False, "description": "upgraded",
                                  "parameters": {"migrate_to_chat_id": -100777}}
        with mock.patch.object(c.session, "post", return_value=resp):
            with self.assertRaises(m.TelegramError) as ctx:
                c.send_message("hi")
        self.assertEqual(ctx.exception.migrate_to, -100777)

    def test_get_updates_requests_needed_update_types(self):
        c = m.TelegramClient("1:abc", "1")
        with mock.patch.object(c, "_call", return_value={"ok": True, "result": [{"update_id": 1}]}) as call:
            self.assertEqual(c.get_updates(7), [{"update_id": 1}])
        data = call.call_args[0][1]
        self.assertEqual(data["offset"], 7)
        self.assertEqual(set(json.loads(data["allowed_updates"])),
                         {"message", "callback_query", "my_chat_member"})
        self.assertIs(call.call_args[1]["session"], c.poll_session)


class ControllerTests(TmpDirCase):
    def setUp(self):
        super().setUp()
        self.tg = FakeTg()
        self.reg = m.AccessRegistry(self.path, ADMIN)
        self.checks = []
        self.ctl = m.BotController(self.tg, self.reg, 60, threading.Event(),
                                   check_now=lambda: self.checks.append(1))

    # ---- конструктори оновлень ----
    @staticmethod
    def msg(uid, text, chat_type="private", chat_id=None, **user):
        return {"message": {"text": text, "chat": {"id": chat_id or uid, "type": chat_type},
                            "from": {"id": uid, "first_name": user.get("name", "Іван"),
                                     "username": user.get("username", "ivan")}}}

    @staticmethod
    def cb(uid, data, mid=77, cb_id="cb1"):
        return {"callback_query": {"id": cb_id, "data": data, "from": {"id": uid},
                                   "message": {"message_id": mid, "chat": {"id": uid}}}}

    @staticmethod
    def mcm(chat_id, actor_id, new, old="left", title="Група", kind="supergroup", actor_name="Хтось"):
        return {"my_chat_member": {
            "chat": {"id": chat_id, "type": kind, "title": title},
            "from": {"id": actor_id, "first_name": actor_name, "username": "someone"},
            "old_chat_member": {"status": old}, "new_chat_member": {"status": new}}}

    def feed(self, upd):
        self.ctl.handle_update(upd)

    def buttons(self, markup):
        return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]

    # ---- доступ для нових користувачів ----
    def test_new_user_start_creates_request_and_pings_admin_with_buttons(self):
        self.feed(self.msg(10, "/start"))
        self.assertTrue(any("Запит" in t for t in self.tg.texts_to(10)))
        admin_msgs = [x for x in self.tg.sent if x[0] == str(ADMIN)]
        self.assertEqual(len(admin_msgs), 1)
        self.assertEqual(self.buttons(admin_msgs[0][2]), ["ok:10", "no:10"])
        self.assertIn("pending", self.reg.data)
        self.assertIn("10", self.reg.data["pending"])
        self.feed(self.msg(10, "/start"))                                   # повторний запит — без спаму
        self.assertEqual(len([x for x in self.tg.sent if x[0] == str(ADMIN)]), 1)
        self.assertTrue(any("уже" in t for t in self.tg.texts_to(10)))

    def test_unknown_user_without_start_gets_hint_only(self):
        self.feed(self.msg(10, "привіт"))
        self.assertEqual(self.reg.counts()["pending"], 0)
        self.assertEqual(self.tg.texts_to(ADMIN), [])
        self.assertTrue(any("/start" in t for t in self.tg.texts_to(10)))

    def test_unknown_user_is_never_a_recipient(self):
        self.feed(self.msg(10, "/start"))
        self.assertNotIn("10", self.reg.recipients())

    def test_admin_approves_via_button(self):
        self.feed(self.msg(10, "/start"))
        self.feed(self.cb(ADMIN, "ok:10"))
        self.assertTrue(self.reg.is_allowed(10))
        self.assertIn("10", self.reg.recipients())
        self.assertTrue(any("Доступ надано" in t for t in self.tg.texts_to(10)))
        self.assertEqual(self.tg.answers[-1][0], "cb1")
        self.assertTrue(self.tg.edits)                                      # екран запитів оновлено

    def test_admin_denies_via_button_and_user_cannot_spam(self):
        self.feed(self.msg(10, "/start"))
        self.feed(self.cb(ADMIN, "no:10"))
        self.assertFalse(self.reg.is_allowed(10))
        self.assertTrue(any("відхилив" in t for t in self.tg.texts_to(10)))
        before = len(self.tg.texts_to(ADMIN))
        self.feed(self.msg(10, "/start"))
        self.assertEqual(len(self.tg.texts_to(ADMIN)), before)              # адмін не отримує повторів

    def test_stale_deny_button_is_harmless(self):
        self.feed(self.cb(ADMIN, "no:999"))
        self.assertEqual(self.tg.answers[-1][1], "Запит уже оброблено")

    def test_non_admin_cannot_use_admin_buttons(self):
        self.feed(self.msg(10, "/start"))
        self.feed(self.cb(10, "ok:10"))
        self.assertFalse(self.reg.is_allowed(10))
        self.assertEqual(self.tg.answers[-1][2], True)                      # alert «лише для адміністратора»

    def test_allowed_user_start_is_acknowledged_and_reactivated(self):
        self.reg.add_user(10)
        self.reg.mark_user_inactive(10)
        self.feed(self.msg(10, "/start"))
        self.assertNotIn("inactive", self.reg.data["users"]["10"])
        self.assertTrue(any("є доступ" in t for t in self.tg.texts_to(10)))

    # ---- панель адміна ----
    def test_admin_start_shows_panel(self):
        self.feed(self.msg(ADMIN, "/start"))
        markup = self.tg.sent[-1][2]
        self.assertEqual(self.buttons(markup), ["pend", "users", "groups", "add", "tgl", "chk"])

    def test_panel_is_not_shown_to_non_admin(self):
        self.feed(self.msg(10, "/admin"))
        self.assertFalse(any("Адмін-панель" in t for _, t, _ in self.tg.sent))

    def test_admin_commands_ignored_in_groups(self):
        self.feed(self.msg(ADMIN, "/start", chat_type="supergroup", chat_id=-200))
        self.feed(self.msg(10, "/start", chat_type="group", chat_id=-200))
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.reg.counts()["pending"], 0)

    def test_add_user_by_id_flow(self):
        self.feed(self.cb(ADMIN, "add"))
        self.feed(self.msg(ADMIN, "не число"))
        self.assertTrue(any("ID має бути" in t for t in self.tg.texts_to(ADMIN)))
        self.assertFalse(self.reg.is_allowed(555))
        self.feed(self.msg(ADMIN, "555"))
        self.assertTrue(self.reg.is_allowed(555))
        self.assertTrue(any("Доступ надано" in t for t in self.tg.texts_to(ADMIN)))
        self.feed(self.msg(ADMIN, "777"))                                    # режим очікування закінчився
        self.assertFalse(self.reg.is_allowed(777))

    def test_add_by_id_cancel_and_own_id(self):
        self.feed(self.cb(ADMIN, "add"))
        self.feed(self.msg(ADMIN, "/cancel"))
        self.feed(self.msg(ADMIN, "555"))
        self.assertFalse(self.reg.is_allowed(555))
        self.feed(self.msg(ADMIN, f"/adduser {ADMIN}"))
        self.assertTrue(any("власний ID" in t for t in self.tg.texts_to(ADMIN)))

    def test_adduser_and_removeuser_commands(self):
        self.feed(self.msg(ADMIN, "/adduser 777"))
        self.assertTrue(self.reg.is_allowed(777))
        self.feed(self.msg(ADMIN, "/removeuser 777"))
        self.assertFalse(self.reg.is_allowed(777))
        self.feed(self.msg(ADMIN, "/adduser"))
        self.assertTrue(any("Використання" in t for t in self.tg.texts_to(ADMIN)))

    def test_add_by_id_reports_when_user_unreachable(self):
        self.tg.always["555"] = m.TelegramError("sendMessage", 403, "Forbidden: bot can't initiate conversation")
        self.feed(self.msg(ADMIN, "/adduser 555"))
        self.assertTrue(self.reg.is_allowed(555))
        self.assertTrue(any("/start" in t for t in self.tg.texts_to(ADMIN)))

    def test_remove_user_with_confirmation(self):
        self.reg.add_user(10, "Іван", "ivan")
        self.feed(self.cb(ADMIN, "rm:10"))
        self.assertTrue(self.reg.is_allowed(10))                             # ще лише підтвердження
        self.assertIn("rmy:10", self.buttons(self.tg.edits[-1][3]))
        self.feed(self.cb(ADMIN, "rmy:10"))
        self.assertFalse(self.reg.is_allowed(10))
        self.assertTrue(any("скасовано" in t for t in self.tg.texts_to(10)))

    def test_toggle_admin_notifications_and_check_now(self):
        self.feed(self.cb(ADMIN, "tgl"))
        self.assertFalse(self.reg.admin_notify)
        self.assertNotIn(str(ADMIN), self.reg.recipients())
        self.feed(self.cb(ADMIN, "tgl"))
        self.assertTrue(self.reg.admin_notify)
        self.feed(self.cb(ADMIN, "chk"))
        self.assertEqual(self.checks, [1])

    def test_texts_escape_html_in_names(self):
        self.feed(self.msg(10, "/start", name="<b>Хакер</b>", username="x"))
        text = [t for c, t, _ in self.tg.sent if c == str(ADMIN)][0]
        self.assertNotIn("<b>Хакер</b>", text)
        self.assertIn("&lt;b&gt;", text)

    # ---- групи: лише адмін ----
    def test_admin_adding_bot_to_group_registers_it(self):
        self.feed(self.mcm(-200, ADMIN, "member"))
        self.assertTrue(self.reg.has_group(-200))
        self.assertIn("-200", self.reg.recipients())
        self.assertEqual(self.tg.left, [])
        self.assertTrue(self.tg.texts_to(-200))                              # привітання в групі
        self.assertTrue(any("підключено" in t for t in self.tg.texts_to(ADMIN)))

    def test_admin_adding_bot_to_channel_as_administrator(self):
        self.feed(self.mcm(-300, ADMIN, "administrator", kind="channel", title="Канал"))
        self.assertEqual(self.reg.data["groups"]["-300"]["type"], "channel")

    def test_stranger_adding_bot_to_group_is_rejected(self):
        self.feed(self.mcm(-200, 4242, "member", actor_name="Стороннiй"))
        self.assertFalse(self.reg.has_group(-200))
        self.assertEqual(self.tg.left, ["-200"])
        self.assertNotIn("-200", self.reg.recipients())
        self.assertTrue(any("заблоковано" in t for t in self.tg.texts_to(ADMIN)))

    def test_leave_event_after_rejection_is_silent(self):
        self.feed(self.mcm(-200, 4242, "member"))
        n = len(self.tg.sent)
        self.feed(self.mcm(-200, 4242, "left", old="member"))
        self.assertEqual(len(self.tg.sent), n)

    def test_bot_removed_from_group_is_unsubscribed(self):
        self.feed(self.mcm(-200, ADMIN, "member"))
        self.feed(self.mcm(-200, 4242, "kicked", old="member"))
        self.assertFalse(self.reg.has_group(-200))
        self.assertTrue(any("прибрано" in t for t in self.tg.texts_to(ADMIN)))

    def test_rights_change_in_registered_group_is_ignored(self):
        self.feed(self.mcm(-200, ADMIN, "member"))
        n = len(self.tg.sent)
        self.feed(self.mcm(-200, 4242, "administrator", old="member"))
        self.assertTrue(self.reg.has_group(-200))
        self.assertEqual(self.tg.left, [])
        self.assertEqual(len(self.tg.sent), n)

    def test_group_removal_via_panel_leaves_chat(self):
        self.feed(self.mcm(-200, ADMIN, "member"))
        self.feed(self.cb(ADMIN, "rg:-200"))
        self.assertTrue(self.reg.has_group(-200))
        self.feed(self.cb(ADMIN, "rgy:-200"))
        self.assertFalse(self.reg.has_group(-200))
        self.assertEqual(self.tg.left, ["-200"])

    def test_supergroup_migration_message_updates_registry(self):
        self.feed(self.mcm(-200, ADMIN, "member", kind="group"))
        self.feed({"message": {"chat": {"id": -200, "type": "group"}, "migrate_to_chat_id": -1000}})
        self.assertTrue(self.reg.has_group(-1000))
        self.assertFalse(self.reg.has_group(-200))

    def test_user_blocking_bot_marks_inactive(self):
        self.reg.add_user(10)
        self.feed({"my_chat_member": {"chat": {"id": 10, "type": "private"}, "from": {"id": 10},
                                      "old_chat_member": {"status": "member"},
                                      "new_chat_member": {"status": "kicked"}}})
        self.assertTrue(self.reg.data["users"]["10"]["inactive"])

    # ---- цикл опитування ----
    def test_run_loop_advances_offset_survives_errors_and_stops(self):
        script = [requests.ConnectionError("blip"),
                  [dict(self.msg(10, "/start"), update_id=5), {"update_id": 6}],
                  m.TelegramError("getUpdates", 409, "Conflict")]
        offsets = []

        class Tg(FakeTg):
            def get_updates(inner, offset=None, timeout=25):
                offsets.append(offset)
                if not script:
                    self.ctl.stop_event.set()
                    return []
                item = script.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return item

            def delete_webhook(inner):
                pass

        self.tg = Tg()
        self.ctl.tg = self.tg
        with mock.patch.object(self.ctl.stop_event, "wait", return_value=False):
            self.ctl.run()
        self.assertEqual(offsets[:3], [None, None, 7])
        self.assertIn("10", self.reg.data["pending"])

    def test_handler_exception_does_not_kill_loop(self):
        calls = []

        class Tg(FakeTg):
            def get_updates(inner, offset=None, timeout=25):
                if calls:
                    self.ctl.stop_event.set()
                    return []
                calls.append(1)
                return [{"update_id": 1, "message": "зламане"}]

            def delete_webhook(inner):
                raise RuntimeError("ignored")

        self.ctl.tg = Tg()
        self.ctl.run()                                                       # не має впасти


if __name__ == "__main__":
    unittest.main()