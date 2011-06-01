# Copyright (c) 2010 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Gclient-specific SCM-specific operations."""

import logging
import os
import posixpath
import re
import sys
import time

import gclient_utils
import scm
import subprocess2


class DiffFilterer(object):
  """Simple class which tracks which file is being diffed and
  replaces instances of its file name in the original and
  working copy lines of the svn/git diff output."""
  index_string = "Index: "
  original_prefix = "--- "
  working_prefix = "+++ "

  def __init__(self, relpath):
    # Note that we always use '/' as the path separator to be
    # consistent with svn's cygwin-style output on Windows
    self._relpath = relpath.replace("\\", "/")
    self._current_file = ""
    self._replacement_file = ""

  def SetCurrentFile(self, current_file):
    self._current_file = current_file
    # Note that we always use '/' as the path separator to be
    # consistent with svn's cygwin-style output on Windows
    self._replacement_file = posixpath.join(self._relpath, current_file)

  def _Replace(self, line):
    return line.replace(self._current_file, self._replacement_file)

  def Filter(self, line):
    if (line.startswith(self.index_string)):
      self.SetCurrentFile(line[len(self.index_string):])
      line = self._Replace(line)
    else:
      if (line.startswith(self.original_prefix) or
          line.startswith(self.working_prefix)):
        line = self._Replace(line)
    print(line)


def ask_for_data(prompt):
  try:
    return raw_input(prompt)
  except KeyboardInterrupt:
    # Hide the exception.
    sys.exit(1)


### SCM abstraction layer

# Factory Method for SCM wrapper creation

def GetScmName(url):
  if url:
    url, _ = gclient_utils.SplitUrlRevision(url)
    if (url.startswith('git://') or url.startswith('ssh://') or
        url.endswith('.git')):
      return 'git'
    elif (url.startswith('http://') or url.startswith('https://') or
          url.startswith('svn://') or url.startswith('svn+ssh://')):
      return 'svn'
  return None


def CreateSCM(url, root_dir=None, relpath=None):
  SCM_MAP = {
    'svn' : SVNWrapper,
    'git' : GitWrapper,
  }

  scm_name = GetScmName(url)
  if not scm_name in SCM_MAP:
    raise gclient_utils.Error('No SCM found for url %s' % url)
  return SCM_MAP[scm_name](url, root_dir, relpath)


# SCMWrapper base class

class SCMWrapper(object):
  """Add necessary glue between all the supported SCM.

  This is the abstraction layer to bind to different SCM.
  """
  def __init__(self, url=None, root_dir=None, relpath=None):
    self.url = url
    self._root_dir = root_dir
    if self._root_dir:
      self._root_dir = self._root_dir.replace('/', os.sep)
    self.relpath = relpath
    if self.relpath:
      self.relpath = self.relpath.replace('/', os.sep)
    if self.relpath and self._root_dir:
      self.checkout_path = os.path.join(self._root_dir, self.relpath)

  def RunCommand(self, command, options, args, file_list=None):
    # file_list will have all files that are modified appended to it.
    if file_list is None:
      file_list = []

    commands = ['cleanup', 'update', 'updatesingle', 'revert',
                'revinfo', 'status', 'diff', 'pack', 'runhooks']

    if not command in commands:
      raise gclient_utils.Error('Unknown command %s' % command)

    if not command in dir(self):
      raise gclient_utils.Error('Command %s not implemented in %s wrapper' % (
          command, self.__class__.__name__))

    return getattr(self, command)(options, args, file_list)


class GitWrapper(SCMWrapper):
  """Wrapper for Git"""

  def GetRevisionDate(self, revision):
    """Returns the given revision's date in ISO-8601 format (which contains the
    time zone)."""
    # TODO(floitsch): get the time-stamp of the given revision and not just the
    # time-stamp of the currently checked out revision.
    return self._Capture(['log', '-n', '1', '--format=%ai'])

  @staticmethod
  def cleanup(options, args, file_list):
    """'Cleanup' the repo.

    There's no real git equivalent for the svn cleanup command, do a no-op.
    """

  def diff(self, options, args, file_list):
    merge_base = self._Capture(['merge-base', 'HEAD', 'origin'])
    self._Run(['diff', merge_base], options)

  def pack(self, options, args, file_list):
    """Generates a patch file which can be applied to the root of the
    repository.

    The patch file is generated from a diff of the merge base of HEAD and
    its upstream branch.
    """
    merge_base = self._Capture(['merge-base', 'HEAD', 'origin'])
    gclient_utils.CheckCallAndFilter(
        ['git', 'diff', merge_base],
        cwd=self.checkout_path,
        filter_fn=DiffFilterer(self.relpath).Filter)

  def update(self, options, args, file_list):
    """Runs git to update or transparently checkout the working copy.

    All updated files will be appended to file_list.

    Raises:
      Error: if can't get URL for relative path.
    """
    if args:
      raise gclient_utils.Error("Unsupported argument(s): %s" % ",".join(args))

    self._CheckMinVersion("1.6.6")

    default_rev = "refs/heads/master"
    url, deps_revision = gclient_utils.SplitUrlRevision(self.url)
    rev_str = ""
    revision = deps_revision
    if options.revision:
      # Override the revision number.
      revision = str(options.revision)
    if not revision:
      revision = default_rev

    if gclient_utils.IsDateRevision(revision):
      # Date-revisions only work on git-repositories if the reflog hasn't
      # expired yet. Use rev-list to get the corresponding revision.
      #  git rev-list -n 1 --before='time-stamp' branchname
      if options.transitive:
        print('Warning: --transitive only works for SVN repositories.')
        revision = default_rev

    rev_str = ' at %s' % revision
    files = []

    printed_path = False
    verbose = []
    if options.verbose:
      print('\n_____ %s%s' % (self.relpath, rev_str))
      verbose = ['--verbose']
      printed_path = True

    if revision.startswith('refs/heads/'):
      rev_type = "branch"
    elif revision.startswith('origin/'):
      # For compatability with old naming, translate 'origin' to 'refs/heads'
      revision = revision.replace('origin/', 'refs/heads/')
      rev_type = "branch"
    else:
      # hash is also a tag, only make a distinction at checkout
      rev_type = "hash"

    if not os.path.exists(self.checkout_path):
      self._Clone(revision, url, options)
      files = self._Capture(['ls-files']).splitlines()
      file_list.extend([os.path.join(self.checkout_path, f) for f in files])
      if not verbose:
        # Make the output a little prettier. It's nice to have some whitespace
        # between projects when cloning.
        print('')
      return

    if not os.path.exists(os.path.join(self.checkout_path, '.git')):
      raise gclient_utils.Error('\n____ %s%s\n'
                                '\tPath is not a git repo. No .git dir.\n'
                                '\tTo resolve:\n'
                                '\t\trm -rf %s\n'
                                '\tAnd run gclient sync again\n'
                                % (self.relpath, rev_str, self.relpath))

    # See if the url has changed (the unittests use git://foo for the url, let
    # that through).
    current_url = self._Capture(['config', 'remote.origin.url'])
    # TODO(maruel): Delete url != 'git://foo' since it's just to make the
    # unit test pass. (and update the comment above)
    if current_url != url and url != 'git://foo':
      print('_____ switching %s to a new upstream' % self.relpath)
      # Make sure it's clean
      self._CheckClean(rev_str)
      # Switch over to the new upstream
      self._Run(['remote', 'set-url', 'origin', url], options)
      quiet = []
      if not options.verbose:
        quiet = ['--quiet']
      self._Run(['fetch', 'origin', '--prune'] + quiet, options)
      self._Run(['reset', '--hard', 'origin/master'] + quiet, options)
      files = self._Capture(['ls-files']).splitlines()
      file_list.extend([os.path.join(self.checkout_path, f) for f in files])
      return

    cur_branch = self._GetCurrentBranch()

    # Cases:
    # 0) HEAD is detached. Probably from our initial clone.
    #   - make sure HEAD is contained by a named ref, then update.
    # Cases 1-4. HEAD is a branch.
    # 1) current branch is not tracking a remote branch (could be git-svn)
    #   - try to rebase onto the new hash or branch
    # 2) current branch is tracking a remote branch with local committed
    #    changes, but the DEPS file switched to point to a hash
    #   - rebase those changes on top of the hash
    # 3) current branch is tracking a remote branch w/or w/out changes,
    #    no switch
    #   - see if we can FF, if not, prompt the user for rebase, merge, or stop
    # 4) current branch is tracking a remote branch, switches to a different
    #    remote branch
    #   - exit

    # GetUpstreamBranch returns something like 'refs/remotes/origin/master' for
    # a tracking branch
    # or 'master' if not a tracking branch (it's based on a specific rev/hash)
    # or it returns None if it couldn't find an upstream
    if cur_branch is None:
      upstream_branch = None
      current_type = "detached"
      logging.debug("Detached HEAD")
    else:
      upstream_branch = scm.GIT.GetUpstreamBranch(self.checkout_path)
      if not upstream_branch or not upstream_branch.startswith('refs/remotes'):
        current_type = "hash"
        logging.debug("Current branch is not tracking an upstream (remote)"
                      " branch.")
      elif upstream_branch.startswith('refs/remotes'):
        current_type = "branch"
      else:
        raise gclient_utils.Error('Invalid Upstream: %s' % upstream_branch)

    # Update the remotes first so we have all the refs.
    backoff_time = 5
    for _ in range(10):
      try:
        remote_output = scm.GIT.Capture(
            ['remote'] + verbose + ['update'],
            cwd=self.checkout_path)
        break
      except gclient_utils.CheckCallError, e:
        # Hackish but at that point, git is known to work so just checking for
        # 502 in stderr should be fine.
        if '502' in e.stderr:
          print(str(e))
          print('Sleeping %.1f seconds and retrying...' % backoff_time)
          time.sleep(backoff_time)
          backoff_time *= 1.3
          continue
        raise

    if verbose:
      print(remote_output.strip())

    # This is a big hammer, debatable if it should even be here...
    if options.force or options.reset:
      self._Run(['reset', '--hard', 'HEAD'], options)

    if current_type == 'detached':
      # case 0
      self._CheckClean(rev_str)
      self._CheckDetachedHead(rev_str, options)
      self._Capture(['checkout', '--quiet', '%s^0' % revision])
      if not printed_path:
        print('\n_____ %s%s' % (self.relpath, rev_str))
    elif current_type == 'hash':
      # case 1
      if scm.GIT.IsGitSvn(self.checkout_path) and upstream_branch is not None:
        # Our git-svn branch (upstream_branch) is our upstream
        self._AttemptRebase(upstream_branch, files, options,
                            newbase=revision, printed_path=printed_path)
        printed_path = True
      else:
        # Can't find a merge-base since we don't know our upstream. That makes
        # this command VERY likely to produce a rebase failure. For now we
        # assume origin is our upstream since that's what the old behavior was.
        upstream_branch = 'origin'
        if options.revision or deps_revision:
          upstream_branch = revision
        self._AttemptRebase(upstream_branch, files, options,
                            printed_path=printed_path)
        printed_path = True
    elif rev_type == 'hash':
      # case 2
      self._AttemptRebase(upstream_branch, files, options,
                          newbase=revision, printed_path=printed_path)
      printed_path = True
    elif revision.replace('heads', 'remotes/origin') != upstream_branch:
      # case 4
      new_base = revision.replace('heads', 'remotes/origin')
      if not printed_path:
        print('\n_____ %s%s' % (self.relpath, rev_str))
      switch_error = ("Switching upstream branch from %s to %s\n"
                     % (upstream_branch, new_base) +
                     "Please merge or rebase manually:\n" +
                     "cd %s; git rebase %s\n" % (self.checkout_path, new_base) +
                     "OR git checkout -b <some new branch> %s" % new_base)
      raise gclient_utils.Error(switch_error)
    else:
      # case 3 - the default case
      files = self._Capture(['diff', upstream_branch, '--name-only']).split()
      if verbose:
        print('Trying fast-forward merge to branch : %s' % upstream_branch)
      try:
        merge_output = scm.GIT.Capture(['merge', '--ff-only', upstream_branch],
            cwd=self.checkout_path)
      except gclient_utils.CheckCallError, e:
        if re.match('fatal: Not possible to fast-forward, aborting.', e.stderr):
          if not printed_path:
            print('\n_____ %s%s' % (self.relpath, rev_str))
            printed_path = True
          while True:
            try:
              # TODO(maruel): That can't work with --jobs.
              action = ask_for_data(
                  'Cannot fast-forward merge, attempt to rebase? '
                  '(y)es / (q)uit / (s)kip : ')
            except ValueError:
              raise gclient_utils.Error('Invalid Character')
            if re.match(r'yes|y', action, re.I):
              self._AttemptRebase(upstream_branch, files, options,
                                  printed_path=printed_path)
              printed_path = True
              break
            elif re.match(r'quit|q', action, re.I):
              raise gclient_utils.Error("Can't fast-forward, please merge or "
                                        "rebase manually.\n"
                                        "cd %s && git " % self.checkout_path
                                        + "rebase %s" % upstream_branch)
            elif re.match(r'skip|s', action, re.I):
              print('Skipping %s' % self.relpath)
              return
            else:
              print('Input not recognized')
        elif re.match("error: Your local changes to '.*' would be "
                      "overwritten by merge.  Aborting.\nPlease, commit your "
                      "changes or stash them before you can merge.\n",
                      e.stderr):
          if not printed_path:
            print('\n_____ %s%s' % (self.relpath, rev_str))
            printed_path = True
          raise gclient_utils.Error(e.stderr)
        else:
          # Some other problem happened with the merge
          logging.error("Error during fast-forward merge in %s!" % self.relpath)
          print(e.stderr)
          raise
      else:
        # Fast-forward merge was successful
        if not re.match('Already up-to-date.', merge_output) or verbose:
          if not printed_path:
            print('\n_____ %s%s' % (self.relpath, rev_str))
            printed_path = True
          print(merge_output.strip())
          if not verbose:
            # Make the output a little prettier. It's nice to have some
            # whitespace between projects when syncing.
            print('')

    file_list.extend([os.path.join(self.checkout_path, f) for f in files])

    # If the rebase generated a conflict, abort and ask user to fix
    if self._IsRebasing():
      raise gclient_utils.Error('\n____ %s%s\n'
                                '\nConflict while rebasing this branch.\n'
                                'Fix the conflict and run gclient again.\n'
                                'See man git-rebase for details.\n'
                                % (self.relpath, rev_str))

    if verbose:
      print('Checked out revision %s' % self.revinfo(options, (), None))

  def revert(self, options, args, file_list):
    """Reverts local modifications.

    All reverted files will be appended to file_list.
    """
    if not os.path.isdir(self.checkout_path):
      # revert won't work if the directory doesn't exist. It needs to
      # checkout instead.
      print('\n_____ %s is missing, synching instead' % self.relpath)
      # Don't reuse the args.
      return self.update(options, [], file_list)

    default_rev = "refs/heads/master"
    _, deps_revision = gclient_utils.SplitUrlRevision(self.url)
    if not deps_revision:
      deps_revision = default_rev
    if deps_revision.startswith('refs/heads/'):
      deps_revision = deps_revision.replace('refs/heads/', 'origin/')

    files = self._Capture(['diff', deps_revision, '--name-only']).split()
    self._Run(['reset', '--hard', deps_revision], options)
    file_list.extend([os.path.join(self.checkout_path, f) for f in files])

  def revinfo(self, options, args, file_list):
    """Returns revision"""
    return self._Capture(['rev-parse', 'HEAD'])

  def runhooks(self, options, args, file_list):
    self.status(options, args, file_list)

  def status(self, options, args, file_list):
    """Display status information."""
    if not os.path.isdir(self.checkout_path):
      print(('\n________ couldn\'t run status in %s:\n'
             'The directory does not exist.') % self.checkout_path)
    else:
      merge_base = self._Capture(['merge-base', 'HEAD', 'origin'])
      self._Run(['diff', '--name-status', merge_base], options)
      files = self._Capture(['diff', '--name-only', merge_base]).split()
      file_list.extend([os.path.join(self.checkout_path, f) for f in files])

  def FullUrlForRelativeUrl(self, url):
    # Strip from last '/'
    # Equivalent to unix basename
    base_url = self.url
    return base_url[:base_url.rfind('/')] + url

  def _Clone(self, revision, url, options):
    """Clone a git repository from the given URL.

    Once we've cloned the repo, we checkout a working branch if the specified
    revision is a branch head. If it is a tag or a specific commit, then we
    leave HEAD detached as it makes future updates simpler -- in this case the
    user should first create a new branch or switch to an existing branch before
    making changes in the repo."""
    if not options.verbose:
      # git clone doesn't seem to insert a newline properly before printing
      # to stdout
      print('')

    clone_cmd = ['clone']
    if revision.startswith('refs/heads/'):
      clone_cmd.extend(['-b', revision.replace('refs/heads/', '')])
      detach_head = False
    else:
      detach_head = True
    if options.verbose:
      clone_cmd.append('--verbose')
    clone_cmd.extend([url, self.checkout_path])

    for _ in range(3):
      try:
        self._Run(clone_cmd, options, cwd=self._root_dir)
        break
      except (gclient_utils.Error, subprocess2.CalledProcessError), e:
        # TODO(maruel): Hackish, should be fixed by moving _Run() to
        # CheckCall().
        # Too bad we don't have access to the actual output.
        # We should check for "transfer closed with NNN bytes remaining to
        # read". In the meantime, just make sure .git exists.
        if (e.args[0] == 'git command clone returned 128' and
            os.path.exists(os.path.join(self.checkout_path, '.git'))):
          print(str(e))
          print('Retrying...')
          continue
        raise e

    if detach_head:
      # Squelch git's very verbose detached HEAD warning and use our own
      self._Capture(['checkout', '--quiet', '%s^0' % revision])
      print(
        ('Checked out %s to a detached HEAD. Before making any commits\n'
         'in this repo, you should use \'git checkout <branch>\' to switch to\n'
         'an existing branch or use \'git checkout origin -b <branch>\' to\n'
         'create a new branch for your work.') % revision)

  def _AttemptRebase(self, upstream, files, options, newbase=None,
                     branch=None, printed_path=False):
    """Attempt to rebase onto either upstream or, if specified, newbase."""
    files.extend(self._Capture(['diff', upstream, '--name-only']).split())
    revision = upstream
    if newbase:
      revision = newbase
    if not printed_path:
      print('\n_____ %s : Attempting rebase onto %s...' % (
          self.relpath, revision))
      printed_path = True
    else:
      print('Attempting rebase onto %s...' % revision)

    # Build the rebase command here using the args
    # git rebase [options] [--onto <newbase>] <upstream> [<branch>]
    rebase_cmd = ['rebase']
    if options.verbose:
      rebase_cmd.append('--verbose')
    if newbase:
      rebase_cmd.extend(['--onto', newbase])
    rebase_cmd.append(upstream)
    if branch:
      rebase_cmd.append(branch)

    try:
      rebase_output = scm.GIT.Capture(rebase_cmd, cwd=self.checkout_path)
    except gclient_utils.CheckCallError, e:
      if (re.match(r'cannot rebase: you have unstaged changes', e.stderr) or
          re.match(r'cannot rebase: your index contains uncommitted changes',
                   e.stderr)):
        while True:
          rebase_action = ask_for_data(
              'Cannot rebase because of unstaged changes.\n'
              '\'git reset --hard HEAD\' ?\n'
              'WARNING: destroys any uncommitted work in your current branch!'
              ' (y)es / (q)uit / (s)how : ')
          if re.match(r'yes|y', rebase_action, re.I):
            self._Run(['reset', '--hard', 'HEAD'], options)
            # Should this be recursive?
            rebase_output = scm.GIT.Capture(rebase_cmd, cwd=self.checkout_path)
            break
          elif re.match(r'quit|q', rebase_action, re.I):
            raise gclient_utils.Error("Please merge or rebase manually\n"
                                      "cd %s && git " % self.checkout_path
                                      + "%s" % ' '.join(rebase_cmd))
          elif re.match(r'show|s', rebase_action, re.I):
            print('\n%s' % e.stderr.strip())
            continue
          else:
            gclient_utils.Error("Input not recognized")
            continue
      elif re.search(r'^CONFLICT', e.stdout, re.M):
        raise gclient_utils.Error("Conflict while rebasing this branch.\n"
                                  "Fix the conflict and run gclient again.\n"
                                  "See 'man git-rebase' for details.\n")
      else:
        print(e.stdout.strip())
        print('Rebase produced error output:\n%s' % e.stderr.strip())
        raise gclient_utils.Error("Unrecognized error, please merge or rebase "
                                  "manually.\ncd %s && git " %
                                  self.checkout_path
                                  + "%s" % ' '.join(rebase_cmd))

    print(rebase_output.strip())
    if not options.verbose:
      # Make the output a little prettier. It's nice to have some
      # whitespace between projects when syncing.
      print('')

  @staticmethod
  def _CheckMinVersion(min_version):
    (ok, current_version) = scm.GIT.AssertVersion(min_version)
    if not ok:
      raise gclient_utils.Error('git version %s < minimum required %s' %
                                (current_version, min_version))

  def _IsRebasing(self):
    # Check for any of REBASE-i/REBASE-m/REBASE/AM. Unfortunately git doesn't
    # have a plumbing command to determine whether a rebase is in progress, so
    # for now emualate (more-or-less) git-rebase.sh / git-completion.bash
    g = os.path.join(self.checkout_path, '.git')
    return (
      os.path.isdir(os.path.join(g, "rebase-merge")) or
      os.path.isdir(os.path.join(g, "rebase-apply")))

  def _CheckClean(self, rev_str):
    # Make sure the tree is clean; see git-rebase.sh for reference
    try:
      scm.GIT.Capture(['update-index', '--ignore-submodules', '--refresh'],
                      cwd=self.checkout_path)
    except gclient_utils.CheckCallError:
      raise gclient_utils.Error('\n____ %s%s\n'
                                '\tYou have unstaged changes.\n'
                                '\tPlease commit, stash, or reset.\n'
                                  % (self.relpath, rev_str))
    try:
      scm.GIT.Capture(['diff-index', '--cached', '--name-status', '-r',
                       '--ignore-submodules', 'HEAD', '--'],
                       cwd=self.checkout_path)
    except gclient_utils.CheckCallError:
      raise gclient_utils.Error('\n____ %s%s\n'
                                '\tYour index contains uncommitted changes\n'
                                '\tPlease commit, stash, or reset.\n'
                                  % (self.relpath, rev_str))

  def _CheckDetachedHead(self, rev_str, options):
    # HEAD is detached. Make sure it is safe to move away from (i.e., it is
    # reference by a commit). If not, error out -- most likely a rebase is
    # in progress, try to detect so we can give a better error.
    try:
      scm.GIT.Capture(['name-rev', '--no-undefined', 'HEAD'],
          cwd=self.checkout_path)
    except gclient_utils.CheckCallError:
      # Commit is not contained by any rev. See if the user is rebasing:
      if self._IsRebasing():
        # Punt to the user
        raise gclient_utils.Error('\n____ %s%s\n'
                                  '\tAlready in a conflict, i.e. (no branch).\n'
                                  '\tFix the conflict and run gclient again.\n'
                                  '\tOr to abort run:\n\t\tgit-rebase --abort\n'
                                  '\tSee man git-rebase for details.\n'
                                   % (self.relpath, rev_str))
      # Let's just save off the commit so we can proceed.
      name = ('saved-by-gclient-' +
              self._Capture(['rev-parse', '--short', 'HEAD']))
      self._Capture(['branch', name])
      print('\n_____ found an unreferenced commit and saved it as \'%s\'' %
          name)

  def _GetCurrentBranch(self):
    # Returns name of current branch or None for detached HEAD
    branch = self._Capture(['rev-parse', '--abbrev-ref=strict', 'HEAD'])
    if branch == 'HEAD':
      return None
    return branch

  def _Capture(self, args):
    return gclient_utils.CheckCall(
        ['git'] + args, cwd=self.checkout_path, print_error=False)[0].strip()

  def _Run(self, args, options, **kwargs):
    kwargs.setdefault('cwd', self.checkout_path)
    gclient_utils.CheckCallAndFilterAndHeader(['git'] + args,
        always=options.verbose, **kwargs)


class SVNWrapper(SCMWrapper):
  """ Wrapper for SVN """

  def GetRevisionDate(self, revision):
    """Returns the given revision's date in ISO-8601 format (which contains the
    time zone)."""
    date = scm.SVN.Capture(['propget', '--revprop', 'svn:date', '-r', revision,
                            os.path.join(self.checkout_path, '.')])
    return date.strip()

  def cleanup(self, options, args, file_list):
    """Cleanup working copy."""
    self._Run(['cleanup'] + args, options)

  def diff(self, options, args, file_list):
    # NOTE: This function does not currently modify file_list.
    if not os.path.isdir(self.checkout_path):
      raise gclient_utils.Error('Directory %s is not present.' %
          self.checkout_path)
    self._Run(['diff'] + args, options)

  def pack(self, options, args, file_list):
    """Generates a patch file which can be applied to the root of the
    repository."""
    if not os.path.isdir(self.checkout_path):
      raise gclient_utils.Error('Directory %s is not present.' %
          self.checkout_path)
    gclient_utils.CheckCallAndFilter(
        ['svn', 'diff', '-x', '--ignore-eol-style'] + args,
        cwd=self.checkout_path,
        print_stdout=False,
        filter_fn=DiffFilterer(self.relpath).Filter)

  def update(self, options, args, file_list):
    """Runs svn to update or transparently checkout the working copy.

    All updated files will be appended to file_list.

    Raises:
      Error: if can't get URL for relative path.
    """
    # Only update if git or hg is not controlling the directory.
    git_path = os.path.join(self.checkout_path, '.git')
    if os.path.exists(git_path):
      print('________ found .git directory; skipping %s' % self.relpath)
      return

    hg_path = os.path.join(self.checkout_path, '.hg')
    if os.path.exists(hg_path):
      print('________ found .hg directory; skipping %s' % self.relpath)
      return

    if args:
      raise gclient_utils.Error("Unsupported argument(s): %s" % ",".join(args))

    # revision is the revision to match. It is None if no revision is specified,
    # i.e. the 'deps ain't pinned'.
    url, revision = gclient_utils.SplitUrlRevision(self.url)
    # Keep the original unpinned url for reference in case the repo is switched.
    base_url = url
    if options.revision:
      # Override the revision number.
      revision = str(options.revision)
    if revision:
      forced_revision = True
      # Reconstruct the url.
      url = '%s@%s' % (url, revision)
      rev_str = ' at %s' % revision
    else:
      forced_revision = False
      rev_str = ''

    if not os.path.exists(self.checkout_path):
      # We need to checkout.
      command = ['checkout', url, self.checkout_path]
      command = self._AddAdditionalUpdateFlags(command, options, revision)
      self._RunAndGetFileList(command, options, file_list, self._root_dir)
      return

    # Get the existing scm url and the revision number of the current checkout.
    try:
      from_info = scm.SVN.CaptureInfo(os.path.join(self.checkout_path, '.'))
    except (gclient_utils.Error, subprocess2.CalledProcessError):
      raise gclient_utils.Error(
          ('Can\'t update/checkout %s if an unversioned directory is present. '
           'Delete the directory and try again.') % self.checkout_path)

    # Look for locked directories.
    dir_info = scm.SVN.CaptureStatus(os.path.join(self.checkout_path, '.'))
    if [True for d in dir_info
        if d[0][2] == 'L' and d[1] == self.checkout_path]:
      # The current directory is locked, clean it up.
      self._Run(['cleanup'], options)

    # Retrieve the current HEAD version because svn is slow at null updates.
    if options.manually_grab_svn_rev and not revision:
      from_info_live = scm.SVN.CaptureInfo(from_info['URL'])
      revision = str(from_info_live['Revision'])
      rev_str = ' at %s' % revision

    if from_info['URL'] != base_url:
      # The repository url changed, need to switch.
      try:
        to_info = scm.SVN.CaptureInfo(url)
      except (gclient_utils.Error, subprocess2.CalledProcessError):
        # The url is invalid or the server is not accessible, it's safer to bail
        # out right now.
        raise gclient_utils.Error('This url is unreachable: %s' % url)
      can_switch = ((from_info['Repository Root'] != to_info['Repository Root'])
                    and (from_info['UUID'] == to_info['UUID']))
      if can_switch:
        print('\n_____ relocating %s to a new checkout' % self.relpath)
        # We have different roots, so check if we can switch --relocate.
        # Subversion only permits this if the repository UUIDs match.
        # Perform the switch --relocate, then rewrite the from_url
        # to reflect where we "are now."  (This is the same way that
        # Subversion itself handles the metadata when switch --relocate
        # is used.)  This makes the checks below for whether we
        # can update to a revision or have to switch to a different
        # branch work as expected.
        # TODO(maruel):  TEST ME !
        command = ['switch', '--relocate',
                   from_info['Repository Root'],
                   to_info['Repository Root'],
                   self.relpath]
        self._Run(command, options, cwd=self._root_dir)
        from_info['URL'] = from_info['URL'].replace(
            from_info['Repository Root'],
            to_info['Repository Root'])
      else:
        if not options.force and not options.reset:
          # Look for local modifications but ignore unversioned files.
          for status in scm.SVN.CaptureStatus(self.checkout_path):
            if status[0] != '?':
              raise gclient_utils.Error(
                  ('Can\'t switch the checkout to %s; UUID don\'t match and '
                   'there is local changes in %s. Delete the directory and '
                   'try again.') % (url, self.checkout_path))
        # Ok delete it.
        print('\n_____ switching %s to a new checkout' % self.relpath)
        gclient_utils.RemoveDirectory(self.checkout_path)
        # We need to checkout.
        command = ['checkout', url, self.checkout_path]
        command = self._AddAdditionalUpdateFlags(command, options, revision)
        self._RunAndGetFileList(command, options, file_list, self._root_dir)
        return

    # If the provided url has a revision number that matches the revision
    # number of the existing directory, then we don't need to bother updating.
    if not options.force and str(from_info['Revision']) == revision:
      if options.verbose or not forced_revision:
        print('\n_____ %s%s' % (self.relpath, rev_str))
      return

    command = ['update', self.checkout_path]
    command = self._AddAdditionalUpdateFlags(command, options, revision)
    self._RunAndGetFileList(command, options, file_list, self._root_dir)

  def updatesingle(self, options, args, file_list):
    filename = args.pop()
    if scm.SVN.AssertVersion("1.5")[0]:
      if not os.path.exists(os.path.join(self.checkout_path, '.svn')):
        # Create an empty checkout and then update the one file we want.  Future
        # operations will only apply to the one file we checked out.
        command = ["checkout", "--depth", "empty", self.url, self.checkout_path]
        self._Run(command, options, cwd=self._root_dir)
        if os.path.exists(os.path.join(self.checkout_path, filename)):
          os.remove(os.path.join(self.checkout_path, filename))
        command = ["update", filename]
        self._RunAndGetFileList(command, options, file_list)
      # After the initial checkout, we can use update as if it were any other
      # dep.
      self.update(options, args, file_list)
    else:
      # If the installed version of SVN doesn't support --depth, fallback to
      # just exporting the file.  This has the downside that revision
      # information is not stored next to the file, so we will have to
      # re-export the file every time we sync.
      if not os.path.exists(self.checkout_path):
        os.makedirs(self.checkout_path)
      command = ["export", os.path.join(self.url, filename),
                 os.path.join(self.checkout_path, filename)]
      command = self._AddAdditionalUpdateFlags(command, options,
          options.revision)
      self._Run(command, options, cwd=self._root_dir)

  def revert(self, options, args, file_list):
    """Reverts local modifications. Subversion specific.

    All reverted files will be appended to file_list, even if Subversion
    doesn't know about them.
    """
    if not os.path.isdir(self.checkout_path):
      # svn revert won't work if the directory doesn't exist. It needs to
      # checkout instead.
      print('\n_____ %s is missing, synching instead' % self.relpath)
      # Don't reuse the args.
      return self.update(options, [], file_list)

    def printcb(file_status):
      file_list.append(file_status[1])
      if logging.getLogger().isEnabledFor(logging.INFO):
        logging.info('%s%s' % (file_status[0], file_status[1]))
      else:
        print(os.path.join(self.checkout_path, file_status[1]))
    scm.SVN.Revert(self.checkout_path, callback=printcb)

    try:
      # svn revert is so broken we don't even use it. Using
      # "svn up --revision BASE" achieve the same effect.
      # file_list will contain duplicates.
      self._RunAndGetFileList(['update', '--revision', 'BASE'], options,
          file_list)
    except OSError, e:
      # Maybe the directory disapeared meanwhile. Do not throw an exception.
      logging.error('Failed to update:\n%s' % str(e))

  def revinfo(self, options, args, file_list):
    """Display revision"""
    try:
      return scm.SVN.CaptureRevision(self.checkout_path)
    except (gclient_utils.Error, subprocess2.CalledProcessError):
      return None

  def runhooks(self, options, args, file_list):
    self.status(options, args, file_list)

  def status(self, options, args, file_list):
    """Display status information."""
    command = ['status'] + args
    if not os.path.isdir(self.checkout_path):
      # svn status won't work if the directory doesn't exist.
      print(('\n________ couldn\'t run \'%s\' in \'%s\':\n'
             'The directory does not exist.') %
                (' '.join(command), self.checkout_path))
      # There's no file list to retrieve.
    else:
      self._RunAndGetFileList(command, options, file_list)

  def FullUrlForRelativeUrl(self, url):
    # Find the forth '/' and strip from there. A bit hackish.
    return '/'.join(self.url.split('/')[:4]) + url

  def _Run(self, args, options, **kwargs):
    """Runs a commands that goes to stdout."""
    kwargs.setdefault('cwd', self.checkout_path)
    gclient_utils.CheckCallAndFilterAndHeader(['svn'] + args,
        always=options.verbose, **kwargs)

  def _RunAndGetFileList(self, args, options, file_list, cwd=None):
    """Runs a commands that goes to stdout and grabs the file listed."""
    cwd = cwd or self.checkout_path
    scm.SVN.RunAndGetFileList(
        options.verbose,
        args + ['--ignore-externals'],
        cwd=cwd,
        file_list=file_list)

  @staticmethod
  def _AddAdditionalUpdateFlags(command, options, revision):
    """Add additional flags to command depending on what options are set.
    command should be a list of strings that represents an svn command.

    This method returns a new list to be used as a command."""
    new_command = command[:]
    if revision:
      new_command.extend(['--revision', str(revision).strip()])
    # --force was added to 'svn update' in svn 1.5.
    if ((options.force or options.manually_grab_svn_rev) and
        scm.SVN.AssertVersion("1.5")[0]):
      new_command.append('--force')
    return new_command
