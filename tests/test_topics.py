"""Topic-model routing: a session lives per (chat, topic); each topic is bound
to a (project, agent) pair, so the same project can run in several topics in
parallel with independent sessions."""
import bot


def setup_function():
    bot.TOPIC_BINDING.clear()
    bot.SESSIONS.clear()


class _Msg:
    def __init__(self, thread_id):
        self.message_thread_id = thread_id


def test_topic_id_defaults_to_zero_for_general_and_private():
    assert bot._topic_id(_Msg(None)) == 0
    assert bot._topic_id(_Msg(42)) == 42


def test_bind_and_resolve_topic_binding():
    bot._bind_topic(100, 5, "personalChef", "generale")
    project = bot._topic_binding(100, 5)[0]
    assert project is not None and project.key == "personalChef"


def test_same_project_two_topics_keep_independent_sessions():
    # "personalChef · backend" (topic 5) and "personalChef · frontend" (topic 6)
    bot._bind_topic(100, 5, "personalChef", "generale")
    bot._bind_topic(100, 6, "personalChef", "generale")
    bot._set_session(100, 5, "sid-backend")
    bot._set_session(100, 6, "sid-frontend")
    assert bot._get_session(100, 5) == "sid-backend"
    assert bot._get_session(100, 6) == "sid-frontend"


def test_unbound_topic_has_no_binding():
    assert bot._topic_binding(100, 99) == (None, None)


def test_bound_topic_resolves_project_and_agent():
    bot._bind_topic(100, 7, "personalChef", "generale")
    project, agent = bot._topic_binding(100, 7)
    assert project.key == "personalChef"
    assert agent is not None and agent.name == "generale"
