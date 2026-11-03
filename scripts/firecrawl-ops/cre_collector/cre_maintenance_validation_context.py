"""Private, process-local evidence and Git witnesses for owned maintenance.

These objects do not authorize a mutation. They retain validated immutable
facts so every boundary can reread their bytes and repeat live Git checks.
"""
import json
import re

from cre_code_handoff_evidence import canonical_path, json_bytes, require, sha
from cre_code_handoff_git import Git


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


class EvidenceSeal:
    """Freeze the complete current evidence closure, never a pass/fail result."""

    def __init__(self, packet, evidence, *, check_aliases=True):
        self.packet = _canonical(packet)
        self.inputs = _canonical(evidence.inputs)
        self.original = dict(evidence.original)
        self.payloads = dict(evidence.original_payloads)
        if not check_aliases:
            return
        paths = [(evidence.inputs[role]['path'], value) for role, value in self.original.items()]
        paths.extend(self.payloads.items())
        seen = {}
        for path, value in paths:
            key = canonical_path(path)
            if len(key) >= 3 and key[1:3] == ':/':
                key = key.casefold()
            require(key not in seen or seen[key] == value, 'conflicting sealed evidence aliases')
            seen[key] = value

    def unchanged(self, packet, evidence):
        require(_canonical(packet) == self.packet and _canonical(evidence.inputs) == self.inputs
                and evidence.original == self.original and evidence.original_payloads == self.payloads,
                'validated evidence closure changed')

    def recheck(self, packet, evidence):
        self.unchanged(packet, evidence)
        evidence.recheck()
        self.unchanged(packet, evidence)


class LoadedMaintenance:
    """A full loader result bound to its original raw packet and evidence object."""

    def __init__(self, raw, packet, evidence):
        self.raw_sha256 = sha(raw)
        self.evidence = evidence
        self.seal = EvidenceSeal(packet, evidence, check_aliases=False)

    def unchanged(self, raw, packet, evidence):
        require(evidence is self.evidence and sha(raw) == self.raw_sha256, 'loaded maintenance proof differs')
        self.seal.unchanged(packet, evidence)

    def extend_after_admission(self, raw, packet, evidence, authorization):
        require(evidence is self.evidence and sha(raw) == self.raw_sha256
                and _canonical(packet) == self.seal.packet and _canonical(evidence.inputs) == self.seal.inputs
                and evidence.original == self.seal.original
                and all(evidence.original_payloads.get(path) == value
                        for path, value in self.seal.payloads.items()),
                'loaded maintenance proof changed during admission')
        review = authorization['review']['path']
        require(review in evidence.original_payloads, 'admitted review bytes missing')
        body = json_bytes(evidence.original_payloads[review])['body']['path']
        require(body in evidence.original_payloads
                and set(evidence.original_payloads) - set(self.seal.payloads) <= {review, body},
                'unexpected evidence added during admission')
        self.seal = EvidenceSeal(packet, evidence)


class RecordingGit(Git):
    """Record read-only queries and replay them; never reuse their results."""

    def __init__(self, native_git):
        super().__init__(native_git)
        self._witnesses = {}
        self._archives = {}

    def record_archive(self, repo, identity, service, expected):
        key = (str(repo), _canonical(identity), service)
        require(key not in self._archives or self._archives[key] == expected,
                'source archive changed during qualification')
        self._archives[key] = expected

    def command(self, repo, *args, input_bytes=None, allowed=(0,)):
        require(args and args[0] in {'rev-parse', 'ls-tree', 'show', 'merge-base'}
                and input_bytes is None and allowed == (0,), 'unsupported recorded Git query')
        result = super().command(repo, *args, input_bytes=input_bytes, allowed=allowed)
        key = (str(repo), tuple(args))
        witness = (len(result), sha(result))
        require(key not in self._witnesses or self._witnesses[key] == witness,
                'Git query changed during qualification')
        self._witnesses[key] = witness
        return result

    def recheck(self):
        from cre_runtime_maintenance_build import _source_archive

        for (repo, args), expected in self._witnesses.items():
            result = super().command(repo, *args)
            require((len(result), sha(result)) == expected, 'Git witness changed')
        for (repo, identity, service), expected in self._archives.items():
            require(sha(_source_archive(repo, json.loads(identity), service)) == expected,
                    'source archive changed')


def qualify_stack_replay(packet, evidence):
    """Perform the full model validation once, then retain its Git witnesses."""
    from cre_current_stack import validate_current_stack

    git = RecordingGit(packet['programs']['native_git'])
    receipt = evidence.obj(packet['continuation']['current_stack'])
    validate_current_stack(packet, evidence, receipt, git=git)
    evidence.recheck()
    return git


_READY_QUALIFIED = object()


class ReadyRecordingGit(RecordingGit):
    """Record only commit-addressed object queries against the reviewed repository."""

    def __init__(self, native_git, repo):
        super().__init__(native_git)
        self.repo = self._repo_key(repo)

    @staticmethod
    def _repo_key(repo):
        value = canonical_path(str(repo))
        return value.casefold() if len(value) >= 3 and value[1:3] == ':/' else value

    def command(self, repo, *args, input_bytes=None, allowed=(0,)):
        from cre_code_handoff_evidence import safe_relative

        require(self._repo_key(repo) == self.repo, 'ready witness repository differs')
        valid = False
        path = None
        if len(args) == 2 and args[0] == 'rev-parse':
            valid = re.fullmatch(r'[0-9a-f]{40}\^\{tree\}', args[1]) is not None
            if not valid and re.fullmatch(r'[0-9a-f]{40}:.+', args[1]):
                valid, path = True, args[1][41:]
        elif len(args) == 2 and args[0] == 'show' and re.fullmatch(r'[0-9a-f]{40}:.+', args[1]):
            valid, path = True, args[1][41:]
        elif len(args) in (4, 6) and args[:3] == ('ls-tree', '-rz', '--full-tree'):
            valid = re.fullmatch(r'[0-9a-f]{40}', args[3]) is not None
            if len(args) == 6:
                valid, path = valid and args[4] == '--', args[5]
        elif len(args) == 4 and args[:2] == ('merge-base', '--is-ancestor'):
            valid = all(re.fullmatch(r'[0-9a-f]{40}', value) for value in args[2:])
        require(valid, 'ready witness must address immutable objects')
        if path is not None:
            safe_relative(path)
        return super().command(repo, *args, input_bytes=input_bytes, allowed=allowed)

    def record_archive(self, repo, identity, service, expected):
        require(self._repo_key(repo) == self.repo, 'ready archive repository differs')
        return super().record_archive(repo, identity, service, expected)


class ReadyReleaseProof:
    """Bind a pre-observation full release qualification to its Git witnesses."""

    def __init__(self, raw, packet, evidence, git, *, _token=None):
        require(_token is _READY_QUALIFIED and type(git) is ReadyRecordingGit,
                'ready release must be fully qualified')
        self.raw = raw
        self.packet = packet
        self.evidence = evidence
        self.seal = EvidenceSeal(packet, evidence)
        self.git = git
        self.native_git = git.native_git
        self.repo = git.repo
        self.witnesses = tuple(git._witnesses.items())
        self.archives = tuple(git._archives.items())

    def recheck(self, raw, packet, evidence, *, current_seal=None):
        require(raw == self.raw and packet is self.packet and evidence is self.evidence,
                'ready qualification identity differs')
        require(type(self.git) is ReadyRecordingGit and self.git.native_git == self.native_git
                and self.git.repo == self.repo
                and tuple(self.git._witnesses.items()) == self.witnesses
                and tuple(self.git._archives.items()) == self.archives,
                'ready Git witness changed')
        require(_canonical(packet) == self.seal.packet and _canonical(evidence.inputs) == self.seal.inputs
                and evidence.original == self.seal.original
                and all(evidence.original_payloads.get(path) == value for path, value in self.seal.payloads.items()),
                'ready qualification closure differs')
        seal = self.seal if current_seal is None else current_seal
        require(type(seal) is EvidenceSeal, 'ready evidence seal differs')
        seal.recheck(packet, evidence)
        self.git.recheck()
        require(tuple(self.git._witnesses.items()) == self.witnesses
                and tuple(self.git._archives.items()) == self.archives,
                'ready Git witness changed during replay')
        seal.unchanged(packet, evidence)


def qualify_ready_release(raw, packet, evidence):
    """Fully validate retained release before fresh observation and record Git checks."""
    from cre_runtime_maintenance import validate_maintenance_release

    git = ReadyRecordingGit(packet['programs']['native_git'], packet['roots']['development'])
    validate_maintenance_release(packet, evidence, git=git)
    evidence.recheck()
    return ReadyReleaseProof(raw, packet, evidence, git, _token=_READY_QUALIFIED)
