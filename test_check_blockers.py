from unittest import TestCase
from mock import Mock, patch

import check_blockers


JUJUBOT_USER = {'login': 'jujubot', 'id': 7779494}
OTHER_USER = {'login': 'user', 'id': 1}

SERIES_LIST = {
    'entries': [
        {'name': 'trunk'},
        {'name': '1.20'},
        {'name': '1.21'},
        {'name': '1.22'},
    ]}


class CheckBlockers(TestCase):

    def test_parse_args(self):
        args = check_blockers.parse_args(['master', '17'])
        self.assertEqual('master', args.branch)
        self.assertEqual('17', args.pull_request)

    def test_get_lp_bugs_with_master_branch(self):
        args = check_blockers.parse_args(['master', '17'])
        with patch('check_blockers.get_json', autospec=True,
                   side_effect=[SERIES_LIST, {'entries': []}]) as gj:
            check_blockers.get_lp_bugs(args)
            gj.assert_called_with((check_blockers.LP_BUGS.format('juju-core')))

    def test_get_lp_bugs_with_supported_branch(self):
        args = check_blockers.parse_args(['1.20', '17'])
        with patch('check_blockers.get_json', autospec=True,
                   side_effect=[SERIES_LIST, {'entries': []}]) as gj:
            check_blockers.get_lp_bugs(args)
            gj.assert_called_with(
                (check_blockers.LP_BUGS.format('juju-core/1.20')))

    def test_get_lp_bugs_with_unsupported_branch(self):
        args = check_blockers.parse_args(['foo', '17'])
        with patch('check_blockers.get_json', autospec=True,
                   side_effect=[SERIES_LIST, {'entries': []}]) as gj:
            check_blockers.get_lp_bugs(args)
        self.assertEqual(1, gj.call_count)
        gj.assert_called_with(check_blockers.LP_SERIES)

    def test_get_lp_bugs_without_blocking_bugs(self):
        args = check_blockers.parse_args(['master', '17'])
        with patch('check_blockers.get_json') as gj:
            empty_bug_list = {'entries': []}
            gj.return_value = empty_bug_list
            bugs = check_blockers.get_lp_bugs(args)
            self.assertEqual({}, bugs)

    def test_get_lp_bugs_with_blocking_bugs(self):
        args = check_blockers.parse_args(['master', '17'])
        bug_list = {
            'entries': [
                {'self_link': 'https://lp/j/98765'},
                {'self_link': 'https://lp/j/54321'},
            ]}
        with patch('check_blockers.get_json', autospec=True,
                   side_effect=[SERIES_LIST, bug_list]):
            bugs = check_blockers.get_lp_bugs(args)
            self.assertEqual(['54321', '98765'], sorted(bugs.keys()))

    def test_get_reason_without_blocking_bugs(self):
        args = check_blockers.parse_args(['master', '17'])
        with patch('check_blockers.get_json') as gj:
            code, reason = check_blockers.get_reason({}, args)
            self.assertEqual(0, code)
            self.assertEqual('No blocking bugs', reason)
            self.assertEqual(0, gj.call_count)

    def test_get_reason_without_comments(self):
        args = check_blockers.parse_args(['master', '17'])
        with patch('check_blockers.get_json') as gj:
            gj.return_value = []
            bugs = {'98765': {'self_link': 'https://lp/j/98765'}}
            code, reason = check_blockers.get_reason(bugs, args)
            self.assertEqual(1, code)
            self.assertEqual('Could not get 17 comments from github', reason)
            gj.assert_called_with((check_blockers.GH_COMMENTS.format('17')))

    def test_get_reason_with_blockers_no_match(self):
        args = check_blockers.parse_args(['master', '17'])
        with patch('check_blockers.get_json') as gj:
            gj.return_value = [{'body': '$$merge$$', 'user': OTHER_USER}]
            bugs = {'98765': {'self_link': 'https://lp/j/98765'}}
            code, reason = check_blockers.get_reason(bugs, args)
            self.assertEqual(1, code)
            self.assertEqual("Does not match ['fixes-98765']", reason)

    def test_get_reason_with_blockers_with_match(self):
        args = check_blockers.parse_args(['master', '17'])
        with patch('check_blockers.get_json') as gj:
            gj.return_value = [
                {'body': '$$merge$$', 'user': OTHER_USER},
                {'body': 'la la __fixes-98765__ ha ha', 'user': OTHER_USER}]
            bugs = {'98765': {'self_link': 'https://lp/j/98765'}}
            code, reason = check_blockers.get_reason(bugs, args)
            self.assertEqual(0, code)
            self.assertEqual("Matches fixes-98765", reason)

    def test_get_reason_with_blockers_with_jujubot_comment(self):
        args = check_blockers.parse_args(['master', '17'])
        with patch('check_blockers.get_json') as gj:
            gj.return_value = [
                {'body': '$$merge$$', 'user': OTHER_USER},
                {'body': 'la la $$fixes-98765$$ ha ha', 'user': JUJUBOT_USER}]
            bugs = {'98765': {'self_link': 'https://lp/j/98765'}}
            code, reason = check_blockers.get_reason(bugs, args)
            self.assertEqual(1, code)
            self.assertEqual("Does not match ['fixes-98765']", reason)

    def test_get_reason_with_blockers_with_reply_jujubot_comment(self):
        args = check_blockers.parse_args(['master', '17'])
        with patch('check_blockers.get_json') as gj:
            gj.return_value = [
                {'body': '$$merge$$', 'user': OTHER_USER},
                {'body': 'Juju bot wrote $$fixes-98765$$', 'user': OTHER_USER}]
            bugs = {'98765': {'self_link': 'https://lp/j/98765'}}
            code, reason = check_blockers.get_reason(bugs, args)
            self.assertEqual(1, code)
            self.assertEqual("Does not match ['fixes-98765']", reason)

    def test_get_reason_with_blockers_with_jfdi(self):
        args = check_blockers.parse_args(['master', '17'])
        with patch('check_blockers.get_json') as gj:
            gj.return_value = [
                {'body': '$$merge$$', 'user': OTHER_USER},
                {'body': 'la la __JFDI__ ha ha', 'user': OTHER_USER}]
            bugs = {'98765': {'self_link': 'https://lp/j/98765'}}
            code, reason = check_blockers.get_reason(bugs, args)
            self.assertEqual(0, code)
            self.assertEqual("Engineer says JFDI", reason)

    def test_get_json(self):
        response = Mock()
        response.read.side_effect = ['{"result": []}']
        with patch('check_blockers.urllib2.urlopen') as urlopen:
            urlopen.return_value = response
            json = check_blockers.get_json("http://api.testing/")
            request = urlopen.call_args[0][0]
            self.assertEqual(request.get_full_url(), "http://api.testing/")
            self.assertEqual(request.get_header("Cache-control"),
                             "max-age=0, must-revalidate")
            self.assertEqual(json, {"result": []})

    def test_get_lp_bugs_url(self):
        self.assertEqual(
            'https://api.launchpad.net/devel/foo/bar?ws.op=searchTasks'
            '&status%3Alist=Confirmed&status%3Alist=Triaged'
            '&status%3Alist=In+Progress&status%3Alist=Fix+Committed'
            '&status%3Alist=Incomplete&importance%3Alist=Critical'
            '&tags%3Alist=blocker&tags_combinator=All',
            check_blockers.get_lp_bugs_url('foo/bar'))
