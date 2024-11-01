import shutil

from .genidx import generate

from lxml import etree
from pathlib import Path
from collections import deque
from typing import List, Callable, Dict
from concurrent.futures import ProcessPoolExecutor, as_completed


class Event:
    def __init__(self, num: int, msg: str, file: str, line: int):
        self.num = num
        self.msg = msg
        self.file = file
        self.line = line


class Report:
    def __init__(self, path: Path, meta: Dict[str, object], events: List[Event]):
        self.path = path
        self.meta = meta
        self.events = events


def _parse_event(file: str, line: int, div: etree._Element) -> Event:
    num = None
    for td in div.iter('td'):
        children = td.getchildren()
        if not children:
            assert num
            return Event(num, td.text, file, line)
        elif 'PathIndex' in children[0].get('class'):
            num = int(children[0].text)
    assert not num
    return Event(0, div.text, file, line)


def _parse_code(file: str, table: etree._Element) -> List[Event]:
    events = []
    line = 0
    for tr in table.getchildren():
        if tr.get('class') == 'codeline':
            line += 1
            continue
        if tr.get('class') == 'variable_popup':
            continue
        div = tr.xpath('td/div[contains(class, msg)]')
        assert div and len(div) == 1
        events.append(_parse_event(file, line, div[0]))
    return events


def _parse_meta(comment: etree.Element, meta: Dict[str, object]) -> bool:
    text = comment.text.strip()
    if text == 'BUGMETAEND':
        return False
    key, value = text.split(' ', 1) if ' ' in text else (text, None)
    if key in ['BUGLINE', 'BUGCOLUMN', 'BUGPATHLENGTH', 'GUESS']:
        value = int(value)
    meta[key] = value
    return True


###############################################################################
# Parse an html report to a Report object.
###############################################################################


def parse_report(report: Path) -> Report:
    events = []
    meta = {}
    is_meta = True
    current_file = None
    tree = etree.parse(report.as_posix(), etree.HTMLParser())
    for element in tree.iter('h4', 'table', etree.Comment):
        if is_meta and element.tag == etree.Comment:
            is_meta = _parse_meta(element, meta)
        elif element.get('class') == 'FileName':
            current_file = element.text
        elif element.get('class') == 'code':
            current_file = current_file or meta['BUGFILE']
            events.extend(_parse_code(current_file, element))
    events.sort(key=lambda e: e.num)
    return Report(report, meta, events)

###############################################################################
# Parse reports in a report directory to a list of Report objects.
###############################################################################


def parse_reports(dir: Path, jobs: int = 1) -> List[Report]:
    reports = find_reports(dir)
    parsed_reports = []
    with ProcessPoolExecutor(jobs) as e:
        futures = [e.submit(parse_report, report) for report in reports]
        for idx, future in enumerate(as_completed(futures), start=1):
            report = future.result()
            parsed_reports.append(report)
            print(f'[{idx}/{len(reports)}] "{report.path.resolve()}"')
    return parsed_reports


class Policy:
    def _handle_memory_leak(report: Report):
        # treat alloc & exception events as important events
        important_events = []
        for event in report.events:
            if ' exception' in event.msg or 'Memory is allocated' in event.msg:
                important_events.append(event)
        return important_events

    def aggressive(report: Report) -> List[Event]:
        # specially handle memory leak report
        if report.meta['BUGTYPE'] == 'Memory leak':
            return Policy._handle_memory_leak(report)
        # only events in the last file are treated as important events
        important_events = []
        important_file = report.events[-1].file
        for event in report.events:
            if (event.file != important_file
                    or event.msg.startswith('Assuming')
                    or event.msg.startswith('Taking')):
                continue
            important_events.append(event)
        return important_events

    def conservative(report: Report) -> List[Event]:
        # every event is treated as an important event
        return report.events


class _Edge:
    def __init__(self, event: Event):
        self.impl = event.msg, event.file, event.line

    def __hash__(self) -> int:
        return hash(self.impl)

    def __eq__(self, o: object) -> bool:
        return isinstance(o, _Edge) and self.impl == o.impl


class _Node(dict):
    def __init__(self):
        self.report = None

    def _make_leaf(self, report: Report):
        self.report = report
        self.clear()

    def _is_leaf(self) -> bool:
        return self.report and not self


class _Tree:
    def __init__(self, policy):
        self.root = _Node()
        self.policy = policy

    def _insert_report(self, report: Report):
        node = self.root
        for event in reversed(self.policy(report)):
            if (node._is_leaf()):
                # discard this longer report
                return
            edge = _Edge(event)
            if edge not in node:
                node[edge] = _Node()
            node = node[edge]
        # discard previous longer reports
        node._make_leaf(report)

    def _unique_reports(self) -> List[Report]:
        reports = []
        queue = deque()
        queue.append(self.root)
        while len(queue):
            node = queue.popleft()
            if node._is_leaf():
                reports.append(node.report)
            else:
                queue.extend(node.values())
        return reports

###############################################################################
# Remove duplicate reports from a Report list.
#
# Two reports are duplicate if IES (important event sequence) of one report
# is a suffix of IES of another report. Report with longer IES is removed.
#
# If two reports have the same IESs, which one to remove is not guaranteed.
#
# Two preset policies (Policy.aggressive & Policy.conservative) to determine
# the important event sequence.
###############################################################################


def unique_reports(reports: List[Report], policy: Callable[[Report], List[Event]]) -> List[Report]:
    tree = _Tree(policy)
    for report in reports:
        tree._insert_report(report)
    return tree._unique_reports()

###############################################################################
# Find report paths in a report directory.
###############################################################################


def find_reports(dir: Path) -> List[Path]:
    reports = []
    if not dir.exists():
        return reports
    for file in dir.glob('*.html'):
        if file.name != 'index.html':
            reports.append(file)
    return reports

###############################################################################
# Copy reports to directory.
###############################################################################


def copy_reports(reports: List[Report], dir: Path):
    dir.mkdir(exist_ok=True, parents=True)
    for report in reports:
        shutil.copy(report.path, dir)


###############################################################################
# Generate index.html in a report directory with specified order.
###############################################################################


def generate_index(dir: Path, ordered_reports: List[Report] = []):
    if not any(dir.iterdir()):
        return
    report_names = list(map(lambda r: r.path.name, ordered_reports))
    generate(dir.as_posix(), report_names)
