from dateutil import parser
import subprocess
import os
import shutil

import numpy as np

from pandas import Series, DataFrame, Panel
from vbench.utils import run_cmd

import logging
log = logging.getLogger('vb.git')

class FailedToBuildError(Exception):
    pass

class Repo(object):

    def __init__(self):
        raise NotImplementedError


class GitRepo(Repo):
    """
    Read some basic statistics about a git repository
    """

    def __init__(self, repo_path):
        log.info("Initializing GitRepo to look at %s" % repo_path)
        self.repo_path = repo_path
        self.git = _git_command(self.repo_path)
        (self.shas, self.messages,
         self.timestamps, self.authors) = self._parse_commit_log()

    @property
    def commit_date(self):
        from pandas.core.datetools import normalize_date
        return self.timestamps.map(normalize_date)

    def _parse_commit_log(self):
        log.debug("Parsing the commit log of %s" % self.repo_path)
        # yoh: using --first-parent so we traverse only the "main"
        #      chain of commits, thus avoiding jumping across possibly
        #      present multiple parallel branches which would introduce
        #      different performance impacts, and in general might be of
        #      no interest (unless they are already merged in the main line)
        # TODO: make it optional
        githist = self.git + ('log --graph  --pretty=format:'
                              '\"::%h::%cd::%s::%an\" --first-parent'
                              '> githist.txt')
        os.system(githist)
        githist = open('githist.txt').read()
        os.remove('githist.txt')

        shas = []
        timestamps = []
        messages = []
        authors = []
        for line in githist.split('\n'):
            # skip commits not in mainline
            if not line[0] == '*':
                continue
            # split line into three real parts, ignoring git-graph in front
            _, sha, stamp, message, author = line.split('::', 4)

            # parse timestamp into datetime object
            stamp = parser.parse(stamp)
            # avoid duplicate timestamps by ignoring them
            # presumably there is a better way to deal with this
            if stamp in timestamps:
                continue

            shas.append(sha)
            timestamps.append(stamp)
            messages.append(message)
            authors.append(author)

        # to UTC for now
        timestamps = _convert_timezones(timestamps)

        shas = Series(shas, timestamps)
        messages = Series(messages, shas)
        timestamps = Series(timestamps, shas)
        authors = Series(authors, shas)
        return shas[::-1], messages[::-1], timestamps[::-1], authors[::-1]

    def get_churn(self, omit_shas=None, omit_paths=None):
        churn = self.get_churn_by_file()

        if omit_paths is not None:
            churn = churn.drop(omit_paths, axis='major')

        if omit_shas is not None:
            churn = churn.drop(omit_shas, axis='minor')

        # sum files and add insertions + deletions
        by_commit = churn.sum('major').sum(1)
        by_date = by_commit.groupby(self.commit_date).sum()
        return by_date

    def get_churn_by_file(self):
        hashes = self.shas.values
        prev = hashes[0]

        insertions = {}
        deletions = {}

        for cur in hashes[1:]:
            i, d = self.diff(cur, prev)
            insertions[cur] = i
            deletions[cur] = d
            prev = cur
        return Panel({'insertions': DataFrame(insertions),
                      'deletions': DataFrame(deletions)},
                     minor_axis=hashes)

    def diff(self, sha, prev_sha):
        cmdline = self.git.split() + ['diff', sha, prev_sha, '--numstat']
        stdout = subprocess.Popen(cmdline, stdout=subprocess.PIPE).stdout

        stdout = stdout.read()

        insertions = {}
        deletions = {}

        for line in stdout.split('\n'):
            try:
                i, d, path = line.split('\t')
                insertions[path] = int(i)
                deletions[path] = int(d)
            except Exception:  # EAFP
                pass

        # statline = stdout.split('\n')[-2]

        # match = re.match('.*\s(.*)\sinsertions.*\s(.*)\sdeletions', statline)

        # insertions = int(match.group(1))
        # deletions = int(match.group(2))
        return insertions, deletions

    def checkout(self, sha):
        pass

    def get_commit_info(self, sha):
        # since all the information is stored in separate series, find
        # that revision first
        sha_where = np.where(self.shas.values==sha)
        if not len(sha_where):
            return None
        if len(sha_where[0])>1:
            log.warning("Found multiple (%d) entries corresponding to %s."
                        % (len(sha_where[0], sha)))
            return None
        i = sha_where[0][0]
        return {'timestamp': self.timestamps[i],
                'sha': sha,
                'message': self.messages[i],
                'authors': self.authors[i]}


class BenchRepo(object):
    """
    Manage an isolated copy of a repository for benchmarking
    """
    def __init__(self, source_url, target_dir, build_cmds, prep_cmd,
                 clean_cmd=None, dependencies=None, always_clean=False):
        self.source_url = source_url
        self.target_dir = target_dir
        self.target_dir_tmp = target_dir + '_tmp'
        self.build_cmds = build_cmds
        self.prep_cmd = prep_cmd
        self.clean_cmd = clean_cmd
        self.dependencies = dependencies
        self.always_clean = always_clean
        self._clean_checkout()
        self._copy_repo()

    def _clean_checkout(self):
        log.debug("Clean checkout of %s from %s"
                  % (self.source_url, self.target_dir_tmp))
        self._clone(self.source_url, self.target_dir_tmp, rm=True)

    def _copy_repo(self):
        log.debug("Repopulating %s" % self.target_dir)
        self._clone(self.target_dir_tmp, self.target_dir, rm=True)
        self._prep()

    def _clone(self, source, target, rm=False):
        log.info("Cloning %s over to %s" % (source, target))
        if os.path.exists(target):
            if rm:
                log.info('Deleting %s first' % target)
                # response = raw_input('%s exists, delete? y/n' % self.target_dir)
                # if response == 'n':
                #     raise Exception('foo')
                # yoh: no need to divert from Python
                #run_cmd('rm -rf %s' % self.target_dir)
                shutil.rmtree(target)
            else:
                raise RuntimeError("Target directory %s already exists. "
                                   "Can't clone into it" % target)
        run_cmd(['git', 'clone', source, target])

    def _copy_benchmark_scripts_and_deps(self):
        pth, _ = os.path.split(os.path.abspath(__file__))
        deps = [os.path.join(pth, 'scripts/vb_run_benchmarks.py')]
        if self.dependencies is not None:
            deps.extend(self.dependencies)

        for dep in deps:
            proc = run_cmd('cp %s %s' % (dep, self.target_dir), shell=True)

    def switch_to_revision(self, rev):
        """
        rev: git SHA
        """
        log.info("Switching to revision %s", rev)
        if self.always_clean:
            self.hard_clean()
        else:
            self._clean()

        self._checkout(rev)
        self._copy_benchmark_scripts_and_deps()
        self._clean_pyc_files()
        self._build()

    def _checkout(self, rev):
        git = _git_command(self.target_dir)
        rest = 'checkout -f %s' % rev
        args = git.split() + rest.split()
        # checkout of a detached commit would always produce stderr
        proc = run_cmd(args, stderr_levels=('debug', 'error'))

    def _build(self):
        cmd = ';'.join([x for x in self.build_cmds.split('\n')
                        if len(x.strip()) > 0])
        proc = run_cmd(cmd, shell=True, cwd=self.target_dir)
        if proc.returncode:
            raise FailedToBuildError(
                "Failed to build. See stderr in the log for details")

    def _prep(self):
        cmd = ';'.join([x for x in self.prep_cmd.split('\n')
                        if len(x.strip()) > 0])
        proc = run_cmd(cmd, shell=True, cwd=self.target_dir)

    def _clean(self):
        if not self.clean_cmd:
            return
        cmd = ';'.join([x for x in self.clean_cmd.split('\n')
                        if len(x.strip()) > 0])
        proc = run_cmd(cmd, shell=True, cwd=self.target_dir)

    def hard_clean(self):
        self._copy_repo()

    def _clean_pyc_files(self, extensions=('.pyc', '.pyo')):
        clean_me = []
        for root, dirs, files in list(os.walk(self.target_dir)):
            for f in files:
                if os.path.splitext(f)[-1] in extensions:
                    clean_me.append(os.path.join(root, f))

        for path in clean_me:
            try:
                os.unlink(path)
            except Exception:
                pass


def _convert_timezones(stamps):
    # tz = config.TIME_ZONE
    def _convert(dt):
        offset = dt.tzinfo.utcoffset(dt)
        dt = dt.replace(tzinfo=None)
        dt = dt - offset
        return dt

    return [_convert(x) for x in stamps]


def _git_command(repo_path):
    return ('git --git-dir=%s/.git --work-tree=%s ' % (repo_path, repo_path))


def get_commit_history():
    # return TimeSeries

    rungithist()

    githist = open('githist.txt').read()
    os.remove('githist.txt')

    sha_date = []
    for line in githist.split('\n'):
        sha_date.append(line.split()[:2])

    return Series(dates, shas), hists


def get_commit_churn(sha, prev_sha):
    # TODO: handle stderr
    stdout = subprocess.Popen(['git', 'diff', sha, prev_sha, '--numstat'],
                              stdout=subprocess.PIPE).stdout
    stdout = stdout.read()

    insertions = {}
    deletions = {}

    for line in stdout.split('\n'):
        try:
            i, d, path = line.split('\t')
            insertions[path] = int(i)
            deletions[path] = int(d)
        except:  # EAFP
            pass

    # statline = stdout.split('\n')[-2]
    # match = re.match('.*\s(.*)\sinsertions.*\s(.*)\sdeletions', statline)
    # insertions = int(match.group(1))
    # deletions = int(match.group(2))
    return insertions, deletions


def get_code_churn(commits):
    shas = commits.index[::-1]

    prev = shas[0]

    insertions = [np.nan]
    deletions = [np.nan]

    insertions = {}
    deletions = {}

    for cur in shas[1:]:
        i, d = get_commit_churn(cur, prev)

        insertions[cur] = i
        deletions[cur] = d

        # insertions.append(i)
        # deletions.append(d)

        prev = cur

    return Panel({'insertions': DataFrame(insertions),
                  'deletions': DataFrame(deletions)}, minor_axis=shas)


    # return DataFrame({'insertions' : insertions,
    #                   'deletions' : deletions}, index=shas)

if __name__ == '__main__':
    repo_path = '/home/wesm/code/pandas'  # XXX:  specific?
    repo = GitRepo(repo_path)
    by_commit = 5
