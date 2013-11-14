import cPickle as pickle
import os
import subprocess

from vbench.git import GitRepo, BenchRepo, FailedToBuildError
from vbench.db import BenchmarkDB
from vbench.utils import multires_order, verify_benchmarks

from datetime import datetime

import logging
log = logging.getLogger('vb.runner')

_RUN_ORDERS = dict(
    normal=lambda x:x,
    reverse=lambda x:x[::-1],
    multires=multires_order,
    )

class BenchmarkRunner(object):
    """

    Parameters
    ----------
    benchmarks : list of Benchmark objects
    repo_path
    build_cmd
    db_path
    run_option : {'eod', 'all', 'last', integer}, default: 'eod'
        eod: use the last revision for each calendar day
        all: benchmark every revision
        last: only try to run the last revision
        some integer N: run each N revisions
    run_order :
        normal : original order (default)
        reverse: in reverse order (latest first)
        multires: cover all revisions but in the order increasing
                  temporal detail
    existing : {'skip', 'min'}
        'skip' : do not re-run the benchmark if already estimated
        'min'  : re-run and store possibly updated better (min)
                 estimate
    dependencies : list or None
        should be list of modules visible in cwd
    """

    def __init__(self, benchmarks, repo_path, repo_url,
                 build_cmd, db_path, tmp_dir,
                 prep_cmd,
                 clean_cmd=None,
                 branches=['master'],
                 run_option='eod', run_order='normal',
                 start_date=None,
                 existing='skip',
                 nochange_rerun_limit=5,
                 module_dependencies=None,
                 always_clean=False,
                 use_blacklist=True,
                 verify=False):
        log.info("Initializing benchmark runner for %d benchmarks" % (len(benchmarks)))
        self._benchmarks = None
        self._checksums = None

        if verify:
            verify_benchmarks(benchmarks, raise_=True)

        self.start_date = start_date
        self.run_option = run_option
        self.run_order = run_order
        assert(existing in ('skip', 'min'))
        self.existing = existing
        self.nochange_rerun_limit = nochange_rerun_limit

        self.repo_path = repo_path
        self.db_path = db_path

        self.repo = GitRepo(self.repo_path, branches=branches)
        self.db = BenchmarkDB(db_path)

        self.use_blacklist = use_blacklist

        # where to copy the repo
        self.tmp_dir = tmp_dir
        self.bench_repo = BenchRepo(repo_url, self.tmp_dir, build_cmd,
                                    prep_cmd,
                                    clean_cmd,
                                    always_clean=always_clean,
                                    dependencies=module_dependencies)

        self.benchmarks = benchmarks

    def _get_benchmarks(self):
        return self._benchmarks

    def _set_benchmarks(self, benchmarks):
        self._benchmarks = benchmarks
        self._checksums = [b.checksum for b in benchmarks]
        self._register_benchmarks()

    benchmarks = property(fget=_get_benchmarks, fset=_set_benchmarks)
    checksums = property(fget=lambda self:self._checksums)

    @property
    def blacklist(self):
        return set(self.db.get_rev_blacklist())

    def _blacklist_rev(self, rev, msg=""):
        if self.use_blacklist:
            log.warn(('Blacklisting %s' % rev) + ": %s" % msg if msg else ".")
            self.db.add_rev_blacklist(rev)

    def run(self):
        log.info("Collecting revisions to run")
        revisions = self._get_revisions_to_run()
        ran_revisions = []
        log.info("Running benchmarks for %d revisions" % (len(revisions),))
        # get the current black list (might be a different one on a next .run())
        blacklist = self.blacklist
        for rev in revisions:
            if self.use_blacklist and rev in blacklist:
                log.warn('Skipping blacklisted %s' % rev)
                continue

            try:
                any_succeeded, n_active = self._run_and_write_results(rev)
            except FailedToBuildError, e:
                self._blacklist_rev(rev, msg=str(e))
                continue

            # All the rerunning below somewhat obscures the destiny of
            # ran_revisions. TODO: make it clear(er)
            ran_revisions.append((rev, (any_succeeded, n_active)))

            if n_active:
                log.debug("%s succeeded among %d active benchmarks",
                          {True: "Some", False: "None"}[any_succeeded],
                          n_active)
                if not any_succeeded:
                    # Give them a second chance
                    self.bench_repo.hard_clean()
                    try:
                        any_succeeded2, n_active2 = self._run_and_write_results(rev)
                    except FailedToBuildError, e:
                        log.warn("Failed to build upon 2nd attempt to benchmark, "
                                 "verify build infrastructure. Skipping for now: %s" % e)
                        continue

                    assert(n_active == n_active2,
                           "Since not any_succeeded, number of benchmarks should remain the same")
                    # just guessing that this revision is broken, should stop
                    # wasting our time
                    if (not any_succeeded2 and n_active > 5):
                        self._blacklist_rev(rev, "None benchmark among %d has succeeded" % n_active)
        return ran_revisions

    def verify_benchmarks(self, rev=None):
        """Verify contained benchmarks
        """
        if rev is not None:
            raise NotImplementedError("Verification is not yet implemented against a preset revision")
        return verify_benchmarks(self.benchmarks)

    def _add_rev_branches(self, rev):
        """Add information about branches for the given revision into DB
        """
        for branch in self.repo.sha_branches[rev]:
            self.db.add_rev_branch(rev=rev, branch=branch)

    def _run_and_write_results(self, rev):
        """
        Returns True if any runs succeeded
        """
        active_benchmarks = self._get_benchmarks_for_rev(rev)

        # Assure up-to-date information about revision->branches
        self._add_rev_branches(rev)

        if not active_benchmarks:
            log.info('No benchmarks need running at %s' % rev)
            return False, 0

        any_succeeded = False

        results = self._run_revision(rev, active_benchmarks)

        self.db.begin_transaction()
        for checksum, timing in results.iteritems():
            timestamp = self.repo.commits.timestamps[rev]

            any_succeeded = any_succeeded or 'timing' in timing

            if self.existing == 'min':
                # verify that we have no information on this benchmark already
                b_prev_results = self.db.get_benchmark_results(checksum, rev=rev)
                assert(len(b_prev_results) < 2) # should be none or just 1 entry
                if len(b_prev_results):
                    old_timing = b_prev_results.ix[0].to_dict()
                    diff = 0
                    if old_timing['timing']:
                        diff = (old_timing['timing'] - timing.get('timing')) / timing['timing']
                    if old_timing['timing'] and diff < 0.005:
                        # we had better result already -- skip saving this one
                        log.debug("Benchmark %s was already timed at %(timing)f - skipping"
                                  % old_timing)
                        # increment no change counter
                        self.db.increment_nochange(checksum, rev=rev)
                        continue
                    else:
                        # we had worse results -- so remove them from
                        # DB in favor of new ones to be introduced
                        log.debug("Benchmark %s was timed before at %(timing)f - deleting"
                                  % old_timing)
                        log.debug("Diff: %.1f%%: %g, %g" % (diff * 100, timing['timing'], old_timing['timing']))
                        self.db.delete_benchmark_results(checksum, rev=rev)
            self.db.write_result(checksum, rev, timestamp,
                                 timing.get('loops'),
                                 timing.get('timing'),
                                 timing.get('traceback'))
        self.db.commit_transaction()

        return any_succeeded, len(active_benchmarks)

    def _register_benchmarks(self):
        log.info('Getting benchmarks')
        ex_benchmarks = self.db.get_benchmarks()
        db_checksums = set(ex_benchmarks.index)
        log.info("Registering %d benchmarks" % len(ex_benchmarks))
        self.db.begin_transaction()
        for bm in self.benchmarks:
            if bm.checksum in db_checksums:
                self.db.update_name(bm)
            else:
                log.info('Writing new benchmark %s, %s' % (bm.name, bm.checksum))
                self.db.write_benchmark(bm)
        self.db.commit_transaction()

    def _run_revision(self, rev, benchmarks):
        # for enhanced logging -- get information about the revision:
        rev_info = self.repo.get_commit_info(rev)
        rev_s = str(rev)
        if rev_info:
            rev_s += ": %(timestamp)s [%(authors)s] %(message)s" % rev_info
        log.info('Running %d benchmarks for revision %s' % (len(benchmarks), rev_s))

        for bm in benchmarks:
            log.debug(bm.name)

        self.bench_repo.switch_to_revision(rev)

        pickle_path = os.path.join(self.tmp_dir, 'benchmarks.pickle')
        results_path = os.path.join(self.tmp_dir, 'results.pickle')
        if os.path.exists(results_path):
            os.remove(results_path)
        pickle.dump(benchmarks, open(pickle_path, 'w'))

        # run the process
        cmd = 'python vb_run_benchmarks.py %s %s' % (pickle_path, results_path)
        log.debug("CMD: %s" % cmd)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                shell=True,
                                cwd=self.tmp_dir)
        stdout, stderr = proc.communicate()

        if stdout:
            log.debug('stdout: %s' % stdout)

        if proc.returncode:
            log.warn("vb_run_benchmark.py returned with non-0 code: %d" % proc.returncode)

        if stderr:
            log.warn("stderr: %s" % stderr)
            if ("object has no attribute" in stderr or
                'ImportError' in stderr):
                log.warn('HARD CLEANING!')
                self.bench_repo.hard_clean()

        if not os.path.exists(results_path):
            log.warn('Failed for revision %s' % rev)
            return {}

        results = pickle.load(open(results_path, 'r'))

        try:
            os.remove(pickle_path)
        except OSError:
            pass

        return results

    def _get_benchmarks_for_rev(self, rev):
        existing_results = self.db.get_rev_results(rev)
        need_to_run = []
        rerun_good_ones = (self.existing == 'min')
        timestamp = self.repo.commits.timestamps[rev]

        for b in self.benchmarks:
            if b.start_date is not None and b.start_date > timestamp:
                continue

            if (b.checksum not in existing_results):
                need_to_run.append(b)
            elif (rerun_good_ones and existing_results[b.checksum]['ncalls']):
                lim = self.nochange_rerun_limit
                ucount = existing_results[b.checksum]['nnochange']
                if ucount < lim:
                    log.debug('Running benchmarks %s as nnochange ' \
                              'counter %d < %d' %  (b.name, ucount, lim))
                    need_to_run.append(b)
                else:
                    # minimum timing has not changed in a while
                    # skip this stable benchmark
                    log.debug('Skipping benchmarks %s as nnochange ' \
                              'counter %d >= %d' % (b.name, ucount, lim))

        return need_to_run

    def _get_revisions_to_run(self):

        # TODO generalize someday to other vcs...git only for now

        # Get a series of shas indexed by the timestamp.
        # yoh: There must be a cleaner way, but my pandas-fu is limited
        rev_by_timestamp = self.repo.commits.reset_index().set_index('timestamps')['shas']

        # # assume they're in order, but check for now
        # assert(rev_by_timestamp.index.is_monotonic)

        if self.start_date is not None:
            rev_by_timestamp = rev_by_timestamp.ix[self.start_date:]

        if self.run_option == 'eod':
            grouped = rev_by_timestamp.groupby(datetime.date)
            revs_to_run = grouped.apply(lambda x: x[-1]).values
        elif self.run_option == 'all':
            revs_to_run = rev_by_timestamp.values
        elif self.run_option == 'last':
            revs_to_run = rev_by_timestamp.values[-1:]
            # TODO: if the very last revision fails, there should be a way
            # to look for the second last, etc, until the last one that was run
        elif isinstance(self.run_option, int):
            revs_to_run = rev_by_timestamp.values[::self.run_option]
        else:
            raise ValueError('unrecognized run_option=%r' % self.run_option)

        if not self.run_order in _RUN_ORDERS:
            raise ValueError('unrecognized run_order=%r. Must be among %s'
                             % (self.run_order, _RUN_ORDERS.keys()))
        revs_to_run = _RUN_ORDERS[self.run_order](revs_to_run)

        return revs_to_run
