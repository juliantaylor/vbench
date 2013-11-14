from pandas import DataFrame

from sqlalchemy import Table, Column, MetaData, create_engine, ForeignKey
from sqlalchemy import types as sqltypes
from sqlalchemy import sql

import logging
log = logging.getLogger('vb.db')

class BenchmarkDB(object):
    """
    Persist vbench results in a sqlite3 database
    """

    def __init__(self, dbpath):
        log.info("Initializing DB at %s" % dbpath)
        self.dbpath = dbpath

        self._engine = create_engine('sqlite:///%s' % dbpath)
        self._metadata = MetaData()
        self._metadata.bind = self._engine

        self._benchmarks = Table('benchmarks', self._metadata,
            Column('checksum', sqltypes.String(32), primary_key=True),
            Column('name', sqltypes.String(200), nullable=False),
            Column('description', sqltypes.Text)
        )
        self._results = Table('results', self._metadata,
            Column('checksum', sqltypes.String(32),
                   ForeignKey('benchmarks.checksum'), primary_key=True),
            Column('revision', sqltypes.String(50), primary_key=True),
            Column('timestamp', sqltypes.DateTime, nullable=False),
            Column('ncalls', sqltypes.String(50)),
            Column('timing', sqltypes.Float),
            Column('traceback', sqltypes.Text),
            Column('nnochange', sqltypes.Integer),
        )

        self._blacklist = Table('blacklist', self._metadata,
            Column('revision', sqltypes.String(50), primary_key=True)
        )

        self._branches = Table('branches', self._metadata,
            Column('branch', sqltypes.String(50), primary_key=True),
            Column('revision', sqltypes.String(50), primary_key=True),
            )
        self._ensure_tables_created()

    _instances = {}

    @classmethod
    def get_instance(cls, dbpath):
        if dbpath not in cls._instances:
            cls._instances[dbpath] = BenchmarkDB(dbpath)
        return cls._instances[dbpath]

    def _ensure_tables_created(self):
        log.debug("Ensuring DB tables are created")
        self._benchmarks.create(self._engine, checkfirst=True)
        self._results.create(self._engine, checkfirst=True)
        self._blacklist.create(self._engine, checkfirst=True)
        self._branches.create(self._engine, checkfirst=True)

    def update_name(self, benchmark):
        """
        benchmarks : list
        """
        table = self._benchmarks
        stmt = (table.update().
                where(table.c.checksum == benchmark.checksum).
                values(checksum=benchmark.checksum))
        self.conn.execute(stmt)

    def increment_nochange(self, checksum, rev):
        """
        increment no change counter of benchmark of revision
        """
        tab = self._results
        select_args = [tab.c.checksum == checksum,
                       tab.c.revision == rev]
        stmt = (tab.update().
                where(sql.and_(*select_args)).
                values(nnochange=tab.c.nnochange + 1))
        self.conn.execute(stmt)

    def restrict_to_benchmarks(self, benchmarks):
        """
        benchmarks : list
        """
        checksums = set([b.checksum for b in benchmarks])

        ex_benchmarks = self.get_benchmarks()

        to_delete = set(ex_benchmarks.index) - checksums

        t = self._benchmarks
        for chksum in to_delete:
            log.info('Deleting %s\n%s' % (chksum, ex_benchmarks.xs(chksum)))
            stmt = t.delete().where(t.c.checksum == chksum)
            self.conn.execute(stmt)

    @property
    def conn(self):
        return self._engine.connect()

    def write_benchmark(self, bm, overwrite=False):
        """

        """
        ins = self._benchmarks.insert()
        ins = ins.values(name=bm.name, checksum=bm.checksum,
                         description=bm.description)
        self.conn.execute(ins)  # XXX: return the result?

    def delete_benchmark(self, checksum):
        """

        """
        raise NotImplementedError
        self.delete_benchmark_results(checksum)
        # TODO -- delete from benchmarks table

    def write_result(self, checksum, revision, timestamp, ncalls,
                     timing, traceback=None, nnochange=0, overwrite=False):
        """

        """
        ins = self._results.insert()
        ins = ins.values(checksum=checksum, revision=revision,
                         timestamp=timestamp,
                         ncalls=ncalls, timing=timing, traceback=traceback,
                         nnochange=nnochange)
        self.conn.execute(ins)  # XXX: return the result?

    def delete_error_results(self):
        tab = self._results
        ins = tab.delete()
        ins = ins.where(tab.c.timing == None)
        self.conn.execute(ins)

    def get_benchmarks(self):
        stmt = sql.select([self._benchmarks])
        result = self.conn.execute(stmt)
        return _sqa_to_frame(result).set_index('checksum')

    def get_rev_results(self, rev):
        tab = self._results
        stmt = sql.select([tab],
                          sql.and_(tab.c.revision == rev))
        results = list(self.conn.execute(stmt))
        return dict((v.checksum, v) for v in results)

    def delete_rev_results(self, rev):
        tab = self._results
        stmt = tab.delete().where(tab.c.revision == rev)
        self.conn.execute(stmt)

    def add_rev_blacklist(self, rev):
        """
        Don't try running this revision again
        """
        stmt = self._blacklist.insert().values(revision=rev)
        self.conn.execute(stmt)

    def get_rev_blacklist(self):
        stmt = self._blacklist.select()
        return [x['revision'] for x in self.conn.execute(stmt)]

    def clear_blacklist(self):
        stmt = self._blacklist.delete()
        self.conn.execute(stmt)

    def add_rev_branch(self, rev, branch):
        if not rev in self.get_branch_revs(branch):
            stmt = self._branches.insert().values(revision=rev, branch=branch)
            self.conn.execute(stmt)

    def get_branches(self):
        # yoh: I bet there is a better way
        stmt = sql.select([self._branches.c.branch])
        return sorted(set([x[0] for x in self.conn.execute(stmt)]))

    def get_branch_revs(self, branch):
        tab = self._branches
        stmt = sql.select([tab.c.revision],
                          sql.and_(tab.c.branch == branch))
        return [x['revision'] for x in self.conn.execute(stmt)]

    def get_benchmark_results(self, checksum, rev=None):
        """

        """
        tab = self._results
        select_args = [tab.c.checksum == checksum]
        if rev: select_args.append(tab.c.revision == rev)
        stmt = sql.select([tab.c.timestamp, tab.c.revision, tab.c.ncalls,
                           tab.c.timing, tab.c.traceback, tab.c.nnochange],
                          sql.and_(*select_args))
        results = self.conn.execute(stmt)

        df = _sqa_to_frame(results).set_index('timestamp')
        return df.sort_index()

    def delete_benchmark_results(self, checksum, rev=None):
        """Delete results for the benchmark. Restrict only to a rev if provided

        """
        tab = self._results
        select_args = [tab.c.checksum == checksum]
        if rev: select_args.append(tab.c.revision == rev)
        stmt = tab.delete().where(sql.and_(*select_args))
        self.conn.execute(stmt)


def _sqa_to_frame(result):
    rows = [tuple(x) for x in result]
    if not rows:
        return DataFrame(columns=result.keys())
    return DataFrame.from_records(rows, columns=result.keys())
