#!/usr/bin/env python
import doctest
import unittest

from zope_test_janitor import *  # noqa


def doctest_DATE_PATTERN():
    r"""

        >>> m = DATE_PATTERN.match('Date: today')
        >>> m is not None
        True
        >>> list(m.groups())
        [u'today']

        >>> m = DATE_PATTERN.match(' Date: today')
        >>> m is None
        True
    """


def doctest_TITLE_PATTERN():
    r"""

        >>> m = TITLE_PATTERN.match('[42] FAIL everything is bad')
        >>> m is not None
        True
        >>> list(m.groups())
        [u'[42] FAIL everything is bad']

        >>> m = TITLE_PATTERN.match(
        ...     'Anything else')
        >>> m is None
        True

    """


def doctest_URL_PATTERN():
    r"""

        >>> m = URL_PATTERN.match(
        ...     ' https://mail.zope.org/pipermail/zope-tests/whatever.html')
        >>> m is not None
        True
        >>> list(m.groups())
        [u'https://mail.zope.org/pipermail/zope-tests/whatever.html']

        >>> m = URL_PATTERN.match(
        ...     'https://mail.zope.org/pipermail/zope-tests/whatever.html')
        >>> m is None
        True

    """


def doctest_Failure_is_buildbot_link():
    """Test for Failure.is_buildbot_link

        >>> f = Failure(None, None)
        >>> f.is_buildbot_link('http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185')
        True

        >>> f.is_buildbot_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/')
        False

    """


def doctest_Failure_parse_buildbot_link():
    """Test for Failure.parse_buildbot_link

        >>> f = Failure(None, None)
        >>> f.parse_buildbot_link('http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185', latest=False)
        (u'http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185', u'185')

        >>> f.parse_buildbot_link('http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/185', latest=True)
        (u'http://winbot.zope.org/builders/z3c.authenticator_py_265_32/builds/-1', u'latest')

    """


def doctest_Failure_is_jenkins_link():
    """Test for Failure.is_jenkins_link

        >>> f = Failure(None, None)
        >>> f.is_jenkins_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/')
        True

    Currently we assume the URL has a trailing slash

        >>> f.is_jenkins_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184')
        False

    """


def doctest_Failure_parse_jenkins_link():
    """Test for Failure.parse_jenkins_link

        >>> f = Failure(None, None)
        >>> f.parse_jenkins_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/', latest=False)
        (u'http://jenkins.starzel.de/job/zopetoolkit_trunk/184/', u'184')

        >>> f.parse_jenkins_link('http://jenkins.starzel.de/job/zopetoolkit_trunk/184/', latest=True)
        (u'http://jenkins.starzel.de/job/zopetoolkit_trunk/lastBuild/', u'latest')

    """


def doctest_Failure_buildbot_source():
    """Test for Failure.buildbot_source

        >>> f = Failure(None, None)
        >>> f.buildbot_source([])

        >>> f.buildbot_source([BuildStep(title='git', link=None, css_class='',
        ...                              text='''
        ... <pre><span class="header">starting git operation</span>
        ... <span class="stderr">From git://github.com/zopefoundation/ZODB
        ...  * branch            HEAD       -&gt; FETCH_HEAD
        ... </span><span class="stdout">HEAD is now at 6b484f8 Correctly quote Windows pathnames
        ... </span><span class="stderr">warning: refname 'HEAD' is ambiguous.
        ... </span><span class="stdout">6b484f8a2ce6cd627139cd6a2c8e9219ecf0ecf2
        ... </span><span class="stderr">warning: refname 'HEAD' is ambiguous.
        ... </span><span class="header">elapsedTime=0.407000</span>
        ... </pre>
        ... ''')])
        GitHubSource(u'zopefoundation/ZODB', u'6b484f8')

    """


def doctest_Report_parse_email():
    r"""

        >>> report = Report()
        >>> report.parse_email([
        ...     'Date: today\n',
        ...     '[1] FAIL: everything\n',
        ...     ' https://mail.zope.org/pipermail/zope-tests/whatever.html\n',
        ... ])
        >>> report.failures
        [Failure(u'[1] FAIL: everything', u'https://mail.zope.org/pipermail/zope-tests/whatever.html')]

    """


def doctest_Report_format_source():
    """Test for Report.format_source

        >>> report = Report()
        >>> report.format_source(None, prefix='lalala')
        u''

        >>> report.format_source(GitHubSource(u'zopefoundation/ZODB', u'6b484f8'), prefix=' (', suffix=')')
        u' (<a href="https://github.com/zopefoundation/ZODB/commit/6b484f8">commit 6b484f8</a>)'

    """


def doctest_Report_format_console_text():
    """Test for Report.format_console_text

        >>> report = Report()
        >>> text = '''
        ... + bin/test
        ... blah blah blah
        ... also <hehe markup> & stuff
        ... when suddenly
        ... Traceback (most recent call last):
        ...   File something something
        ...     code code
        ... Exception: something happen!
        ... and continued
        ... '''
        >>> print(report.format_console_text(text))
        <pre>
        <span class="section">+ bin/test</span>
        blah blah blah
        also &lt;hehe markup&gt; &amp; stuff
        when suddenly
        <span class="error">Traceback (most recent call last):
          File something something
            code code
        Exception: something happen!</span>
        and continued
        </pre>

    """


def doctest_Report_split_to_sections():
    """Test for Report.split_to_sections

        >>> report = Report()
        >>> text = '''
        ... blah
        ... <span class="section">+ bin/test</span>
        ... blah blah blah
        ... more blah
        ... <span class="section">+ bin/test --more</span>
        ... blah blah
        ... etc.
        ... '''.lstrip()
        >>> from pprint import pprint
        >>> pprint(report.split_to_sections(text.splitlines()), width=40)
        [[u'blah'],
         [u'<span class="section">+ bin/test</span>',
          u'blah blah blah',
          u'more blah'],
         [u'<span class="section">+ bin/test --more</span>',
          u'blah blah',
          u'etc.']]

    """


def doctest_Report_collapsed_text():
    r"""Test for Report.collapsed_text

        >>> report = Report()
        >>> print(report.collapsed_text(['a\n', 'b\n', 'c\n']))
        <span class="collapsible collapsed">(3 more lines)</span><article>a
        b
        c
        </article>

    """


def doctest_Report_truncate_pre():
    r"""Test for Report.truncate_pre

        >>> report = Report()
        >>> pre = '''<pre>Here
        ... is
        ... some
        ... text:
        ... a
        ... b
        ... c
        ... d
        ... e</pre>
        ...
        ...
        ... '''
        >>> print(report.truncate_pre(pre, first=4, min_middle=1, last=1))
        <pre>Here
        is
        some
        text:
        <span class="collapsible collapsed">(4 more lines)</span><article>a
        b
        c
        d
        </article>e</pre>

    """


def test_suite():
    return doctest.DocTestSuite(optionflags=doctest.REPORT_NDIFF)


if __name__ == '__main__':
    unittest.main(defaultTest='test_suite')
