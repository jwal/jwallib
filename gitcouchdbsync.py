# Copyright 2011 James Ascroft-Leigh

"""\
%prog [options] [GIT_URL] COUCHDB_URL

I copy objects from GIT_URL and put them into COUCHDB_URL.  To copy
objects in the other direction try couchdbgitsync.py.

A special couchdb document called git-branches is fully mutable and is
updated from the list of branches in the git repository.  Each branch
is then given a different document named after that branch
e.g. git-branch-:branch.  These branch document are also fully mutable
- and there is no equivalent to the reflog yet.  No couchdb documents
are ever deleted.  Other documents for the commits, blobs and trees
are, in theory, are immutable.

Objects are copied in dependency order i.e. the presence of an object
implied that, recursively, the objects it refers to are also present.
This is an assumption that the synchronizer relies upon in order to do
incremental copies.
"""

# Implementation note: The replication in dependency order requires a
# long stack of dependencies to be maintained.  The length of this
# stack is (according to my intuition) of the order of the length of
# the commit history multiplied by the average number of files and
# directories in the repository over time.  If the length of this
# stack becomes a burden then it can safely be discarded as long as
# the root element (the list of branches) is retained.  By also
# retaining the bottom most element you will eventually get everything
# pulled.
# 
#     assert MAX_PULL_STACK_LENGTH > 10, MAX_PULL_STACK_LENGTH
#     if len(pull_stack) > MAX_PULL_STACK_LENGTH:
#         pull_stack[:] = [pull_stack[0]] + [pull_stack[-1]]

from __future__ import with_statement

from cStringIO import StringIO
from collections import namedtuple
from encoding import encode_as_c_identifier
from hashlib import sha1
from jwalutil import trim, read_lines, get1, is_text
from pprint import pformat
from process import call
from couchdblib import get, put, put_update
from posixutils import octal_to_symbolic_mode, symbolic_to_octal_mode
import base64
import contextlib
import json
import optparse
import os
import posixpath
import pycurl as curl
import string
import sys
import time

def git_show(git, sha, attr):
    try:
        start = "#start#"
        end = "#end#"
        out = call(git + ["show", 
                          "--format=format:%s%s%s" % (start, attr, end),
                          "--quiet", sha], do_check=False)
        sindex = out.index(start) + len(start)
        # TODO: Shouldn't need these magic markers
        eindex = out.index(end)
        assert sindex <= eindex, out
        return out[sindex:eindex]
    except Exception:
        raise Exception("Unable to get git attribute %r from %r" % (attr, sha))

def resolve_document_using_git(git, docref):
    document = docref_to_dict(docref)
    kind = docref.kind
    id = docref.id
    get = lambda a: git_show(git, docref.name, a)
    if kind == "branches":
        branches = set()
        for line in read_lines(call(git + ["branch", "-a"])):
            if line.startswith("  "):
                line = trim(line, prefix="  ")
            elif line.startswith("* "):
                line = trim(line, prefix="* ")
                if line == "(no branch)":
                    continue
            else:
                raise NotImplementedError(line)
            if " -> " in line:
                ba, bb = line.split(" -> ", 1)
                branches.add(posixpath.basename(ba))
                branches.add(posixpath.basename(bb))
            else:
                branches.add(posixpath.basename(line))
        branches = list(sorted(b for b in branches if b != "HEAD"))
        document["branches"] = []
        for branch in branches:
            document["branches"].append(docref_to_dict(BranchDocref(branch)))
    elif kind == "branch":
        sha = get1(
            read_lines(
                call(git + ["rev-parse", docref.name])))
        document["commit"] = docref_to_dict(ShaDocRef("commit", sha))
    elif kind == "commit":
        document.update(
            {"author": {"name": get("%an"),
                        "email": get("%ae"),
                        "date": get("%ai")},
             "committer": {"name": get("%cn"),
                           "email": get("%ce"),
                           "date": get("%ci")},
             "message": get("%B"),
             "tree": docref_to_dict(ShaDocRef("tree", get("%T"))),
             "parents": [],
             })
        for p in sorted(get("%P").split(" ")):
            if p == "":
                continue
            document["parents"].append(
                docref_to_dict(ShaDocRef("commit", p)))
    elif kind == "tree":
        document["children"] = []
        for line in read_lines(call(git + ["ls-tree", docref.name])):
            child_mode, child_kind, rest = line.split(" ", 2)
            child_sha, child_basename = rest.split("\t", 1)
            ref = {"child": docref_to_dict(ShaDocRef(child_kind, child_sha)),
                   "basename": child_basename,
                   "mode": octal_to_symbolic_mode(child_mode)}
            document["children"].append(ref)
        document["children"].sort(key=lambda a: a["child"]["sha"])
    elif kind == "blob":
        blob = call(git + ["show", docref.name], do_crlf_fix=False)
        if is_text(blob):
            document["encoding"] = "raw"
            document["raw"] = blob
        else:
            document["encoding"] = "base64"
            document["base64"] = base64.b64encode(blob)
    else:
        raise NotImplementedError(kind)
    return document

DocRef = namedtuple("DocRef", ["id", "kind", "name"])

def BranchDocref(branch):
    branch = unicode(branch)
    return DocRef("git-branch-" + branch, "branch", branch)

def ShaDocRef(kind, sha):
    kind = unicode(kind)
    sha = unicode(sha)
    assert kind in ("tree", "blob", "commit"), kind
    assert len(sha) == len(sha1().hexdigest()), repr(sha)
    return DocRef("git-" + kind + "-" + sha, kind, sha)

def id_to_docref(id):
    most = trim(id, prefix="git-")
    if most == "branches":
        return BRANCHES_DOCREF
    kind, name = most.split("-", 1)
    assert kind in ("branch", "tree", "commit", "blob"), repr(id)
    return DocRef(id, kind, name)

BRANCHES_DOCREF = DocRef(u"git-branches", u"branches", None)

def docref_to_dict(docref):
    if docref.kind == "branch":
        return {"_id": docref.id,
                "type": "git-" + docref.kind,
                "branch": docref.name}
    elif docref.kind == "branches":
        assert docref.name is None, docref
        return {"_id": docref.id,
                "type": "git-" + docref.kind}
    elif docref.kind in ("tree", "commit", "blob"):
        return {"_id": docref.id,
                "type": "git-" + docref.kind,
                "sha": docref.name}
    else:
        raise NotImplementedError(docref)

def dict_to_docref(document):
    id = document["_id"]
    kind = trim(document["type"], prefix="git-")
    if kind == "branches":
        return BRANCHES_DOCREF
    elif kind == "branch":
        return BranchDocref(document["branch"])
    elif kind in ("tree", "commit", "blob"):
        return ShaDocRef(trim(document["type"], prefix="git-"), 
                         document["sha"])
    else:
        raise NotImplementedError(document)

def find_dependencies(document):
    kind = trim(document["type"], prefix="git-")
    if kind == "branches":
        for branch in document["branches"]:
            yield dict_to_docref(branch)
    elif kind == "branch":
        yield dict_to_docref(document["commit"])
    elif kind == "commit":
        for parent in document["parents"]:
            yield dict_to_docref(parent)
        yield dict_to_docref(document["tree"])
    elif kind == "blob":
        pass
    elif kind == "tree":
        for child in document["children"]:
            yield dict_to_docref(child["child"])
    else:
        raise NotImplementedError(document)

BIG_NUMBER = 100000
SMALL_NUMBER = BIG_NUMBER // 2
assert BIG_NUMBER > SMALL_NUMBER, (BIG_NUMBER, SMALL_NUMBER)
assert SMALL_NUMBER > 0, SMALL_NUMBER

MUTABLE_TYPES = ("branches", "branch")

def fetch_all(resolve_document, couchdb_url, seeds):
    to_fetch = list(seeds)
    push = lambda x: to_fetch.append(x)
    pop = lambda: to_fetch.pop()
    def priority_sort_key(docref):
        priority_items = ["commit", "tree"]
        i = dict((a, idx) for (idx, a) in enumerate(priority_items)).get(
            docref.kind, len(priority_items))
        return (i, docref.name, docref)
    def multipush(many, limit=None):
        for i, item in enumerate(reversed(
                sorted(many, key=priority_sort_key))):
            if limit is not None and i > limit:
                break
            push(item)
    multipush(seeds)
    mutable_buffer = {}
    local_buffer = {}
    fetched = set()
    for match in get(couchdb_url + "/_all_docs")["rows"]:
        if not match["id"].startswith("git-"):
            continue
        docref = id_to_docref(match["id"])
        if docref.kind not in MUTABLE_TYPES:
            fetched.add(docref)
    while len(to_fetch) > 0:
        docref = pop()
        if docref not in fetched:
            document = local_buffer.get(docref)
            if document is None:
                print "get", len(to_fetch), docref
                document = resolve_document(docref)
                local_buffer[docref] = document
                if docref.kind in ("branches", "branch"):
                    mutable_buffer[docref] = document
            local_dependencies = set(find_dependencies(document)) - fetched
            if len(local_dependencies) == 0:
                force_couchdb_put(couchdb_url, document)
                del local_buffer[docref]
                fetched.add(docref)
                print "put", len(to_fetch), docref
            else:
                push(docref)
                multipush(local_dependencies)
        if len(local_buffer) > BIG_NUMBER:
            local_buffer.clear()
            local_buffer.update(mutable_buffer)
        assert BIG_NUMBER > 15
        if len(to_fetch) > BIG_NUMBER:
            to_keep = to_fetch[:-SMALL_NUMBER]
            to_fetch[:] =  []
            multipush(seeds)
            multipush(to_keep)
            if len(to_fetch) > BIG_NUMBER:
                print "ouch, lots of seeds?"
            assert len(to_fetch) > 0

def force_couchdb_put(couchdb_url, *documents):
    for document in documents:
        put_update(posixpath.join(couchdb_url, document["_id"]),
                   lambda _: document)

def git_to_couchdb(cache_root, git_url, couchdb_url):
    if git_url is None:
        git = ["git"]
    else:
        cache_dir = os.path.join(cache_root, encode_as_c_identifier(git_url))
        git = ["bash", "-c", 'cd "$1" && shift && exec "$@"', "-", cache_dir, 
               "git"]
        call(["mkdir", "-p", cache_dir])
        call(git + ["init"])
        for r in read_lines(call(git + ["remote"])):
            call(git + ["remote", "rm", r])
        call(git + ["remote", "add", "origin", git_url])
        call(git + ["fetch", "origin"], stdout=None, stderr=None)
    resolve_document = lambda d: resolve_document_using_git(git, d)
    fetch_all(resolve_document, couchdb_url, [BRANCHES_DOCREF])

def main(argv):
    parser = optparse.OptionParser(__doc__)
    parser.add_option("--once", dest="mode", action="store_const",
                      const="once", default="once")
    parser.add_option("--poll", dest="mode", action="store_const",
                      const="poll", default="once")
    parser.add_option("--poll-interval", dest="poll_interval",
                      type=int, default=60*60, 
                      help="unit: seconds, default: hourly")
    parser.add_option("--cache-root", dest="cache_root") 
    options, args = parser.parse_args(argv)
    if len(args) == 0:
        parser.error("Missing: COUCHDB_URL")
    couchdb_url = args.pop()
    if len(args) == 0:
        git_url = None
    else:
        git_url = args.pop()
    if len(args) > 0:
        parser.error("Unexpected: %r" % (args,))
    cache_root = options.cache_root
    if cache_root is None:
        cache_root = "/tmp/gitcouchsynccache"
    cache_root = os.path.abspath(cache_root)
    if options.mode == "once":
        git_to_couchdb(cache_root, git_url, couchdb_url)
    elif options.mode == "poll":
        while True:
            git_to_couchdb(cache_root, git_url, couchdb_url)
            time.sleep(options.poll_interval)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
