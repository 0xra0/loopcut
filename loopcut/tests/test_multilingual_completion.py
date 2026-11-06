"""Synthetic announcements, questions and deferred work in several languages."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_agent_loop as loop_tests
from loopcut import completion_guard, llm


ANNOUNCEMENTS = (
    "Сейчас я добавлю куб.",
    "Теперь создам материал и применю его к сфере.",
    "次にキューブを追加します。",
    "まずシーンを確認します。続いてライトを配置します。",
    "Ahora voy a crear un cubo.",
    "A continuación añadiré una luz.",
    "سأضيف الآن مكعبا.",
    "الآن سأفحص المشهد.",
    "Nu vil jeg tilføje en kube.",
    "Jeg vil nu kontrollere scenen.",
)
QUESTIONS_AND_HANDOFFS = (
    "Добавить куб? Сейчас я создам материал.",
    "Если подтвердите, я добавлю куб.",
    "Сейчас я добавлю куб. Выберите цвет.",
    "Я добавлю куб завтра.",
    "Сейчас я добавлю куб. Какой цвет выбрать",
    "キューブを追加しますか？次にライトを追加します。",
    "確認してください。次にキューブを追加します。",
    "必要ならキューブを追加します。",
    "明日キューブを追加します。",
    "キューブを追加します。どの色がいいですか。",
    "¿Quieres una luz? Ahora voy a crear un cubo.",
    "Si confirmas, añadiré una luz.",
    "Ahora voy a crear un cubo. Dime qué color prefieres.",
    "Mañana voy a crear un cubo.",
    "Ahora voy a crear un cubo. Qué color prefieres",
    "هل تريد مكعبا؟ سأضيف الآن ضوءا.",
    "إذا وافقت سأضيف مكعبا.",
    "سأضيف الآن مكعبا. اختر اللون.",
    "سأضيف مكعبا غدا.",
    "سأضيف الآن مكعبا. ما اللون الذي تريده",
    "Skal jeg tilføje en kube? Nu vil jeg kontrollere scenen.",
    "Hvis du bekræfter, vil jeg tilføje en kube.",
    "Nu vil jeg tilføje en kube. Vælg en farve.",
    "Jeg vil tilføje en kube i morgen.",
    "Nu vil jeg tilføje en kube. Hvilken farve foretrækker du",
)


class MultilingualCompletionTest(unittest.TestCase):
    run_replies = loop_tests.UnrunCompletionTest.run_replies

    def test_announcements_continue_in_each_language(self):
        for text in ANNOUNCEMENTS:
            with self.subTest(text=text):
                self.assertTrue(completion_guard.announces_step(text))

    def test_questions_conditions_and_handoffs_wait(self):
        for text in QUESTIONS_AND_HANDOFFS:
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_japanese_sentence_boundaries_limit_the_lookback(self):
        self.assertFalse(completion_guard.announces_step("キューブを追加します。これは例です。説明は以上です。"))

    def test_conditional_offers_wait_for_the_user_in_each_language(self):
        for text in ("必要であればライトを追加します。", "希望があればライトを調整します。",
                     "選んだら色を変更します。", "ご希望の位置に配置します。",
                     "Ajustaré la luz que prefieras.", "Crearé lo que quieras.",
                     "سأضيف ضوءا عند طلبك.", "سأضبط الضوء حسب رغبتك.",
                     "Jeg vil tilføje lys efter dine ønsker.", "Создам свет по запросу.",
                     "При необходимости добавлю свет."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_optional_work_waits_in_each_language(self):
        for text in ("必要に応じてライトを追加します。", "お好みに合わせてライトを配置します。",
                     "Добавлю свет по желанию.", "Создам материал на ваш вкус.",
                     "Ajustaré el material a tu gusto.", "Añadiré luces según necesites.",
                     "سأضبط اللون حسب الحاجة.", "سأضيف ضوءا حسب ذوقك.",
                     "Jeg vil tilføje lys efter behov.", "Jeg vil justere farven som du ønsker.",
                     "希望があればキューブを追加します。", "Если захотите, создам материал.",
                     "Si deseas, crearé un material.", "إن أردت سأضيف مكعبا.",
                     "Hvis du har lyst, vil jeg tilføje lys."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_finished_first_sentence_stops_optional_followups(self):
        for text in ("Стул готов. Добавлю лампу по желанию.",
                     "椅子は完成しました。お好みに合わせてライトを追加します。",
                     "La silla está lista. Añadiré una luz a tu gusto.",
                     "الكرسي جاهز. سأضيف ضوءا حسب الحاجة.",
                     "Stolen er klar. Jeg vil tilføje lys efter behov.",
                     "Стул готов. Сейчас добавлю лампу.",
                     "椅子は完成しました。次にライトを追加します。",
                     "La silla está lista. Ahora voy a crear una luz.",
                     "الكرسي جاهز. سأضيف الآن ضوءا.",
                     "Stolen er klar. Nu vil jeg tilføje lys.",
                     "The chair is ready. I'll now add a light.",
                     "I'll now add a light if you want."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_finished_deliverables_accept_additional_local_forms(self):
        for text in ("パネルが完成しました。次にライトを追加します。",
                     "Malla lista. Ahora añadiré un cubo.",
                     "Сегмент сделан. Сейчас добавлю сферу.",
                     "Сфера сделана. Сейчас добавлю свет.",
                     "Кольцо сделано. Сейчас создам материал.",
                     "Панели сделаны. Сейчас проверю освещение."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_additional_preference_and_as_needed_phrases_wait(self):
        for text in ("Ajustaré el brillo según lo necesites.",
                     "Создам узор как вам нравится.", "Добавлю детали по мере необходимости.",
                     "好みに合わせてライトを調整します。", "سأضبط الإضاءة كما تريد."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_english_announcements_and_handoffs_keep_their_existing_behavior(self):
        for text, expected in (("I'll now add the chairs.", True),
                               ("I'll add more detail as needed.", True),
                               ("I'll now create a label saying listo.", True),
                               ("The table is ready.", False),
                               ("The table is ready. I'll now add a light.", False),
                               ("I'll now add a light if you want.", False),
                               ("Would you like a light?", False)):
            with self.subTest(text=text):
                self.assertEqual(completion_guard.announces_step(text), expected)

    def test_completed_work_and_old_conditions_do_not_hide_a_new_step(self):
        for text in ("Готово: куб создан. Сейчас проверю освещение.",
                     "Listo, el cubo está creado. Ahora voy a revisar la luz.",
                     "完了しました。次にライトを追加します。",
                     "Если нужен куб, он уже создан. Материал готов. Сейчас добавлю свет.",
                     "Si hacía falta un cubo, ya está. El material está aplicado. Ahora voy a crear una luz.",
                     "Hvis en kube var nødvendig, er den oprettet. Materialet er klart. Nu vil jeg tilføje lys.",
                     "I'll now set the Si wafer material.", "I'll now create a label saying listo."):
            with self.subTest(text=text):
                self.assertTrue(completion_guard.announces_step(text))

    def test_a_final_completion_report_still_ends_the_reply(self):
        for text in ("Сейчас создам куб. Готово.", "キューブを追加します。完了しました。",
                     "Ahora voy a crear un cubo. Listo.", "سأضيف مكعبا. انتهيت.",
                     "Nu vil jeg tilføje en kube. Færdig."):
            with self.subTest(text=text):
                self.assertFalse(completion_guard.announces_step(text))

    def test_same_budget_applies_across_languages_and_resume(self):
        session, turn, requests, _ = self.run_replies([
            llm.Completion(text=ANNOUNCEMENTS[0]), llm.LLMError("Disconnected", code=llm.CONNECTION_LOST)])
        request = session["messages"][0]
        self.assertEqual((requests, request.get(completion_guard.CONTINUED)), (2, 1))
        call = loop_tests.UnrunCompletionTest.call
        session, _, requests, ran = self.run_replies([
            call(), llm.Completion(text=ANNOUNCEMENTS[2]), call(), llm.Completion(text=ANNOUNCEMENTS[4])],
            session=session, resume=True)
        self.assertEqual((requests, ran), (4, 2))
        self.assertEqual(request[completion_guard.CONTINUED], completion_guard.MAX_CONTINUES)


if __name__ == "__main__":
    unittest.main()
