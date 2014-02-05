#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2013  Marek Marczykowski-Górecki <marmarek@invisiblethingslab.com>
# Copyright (C) 2013  Olivier Médoc <o_medoc@yahoo.fr>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
#

from qubes import QubesException,QubesVmCollection
from qubes import QubesVmClasses
from qubes import system_path,vm_files
from qubesutils import size_to_human, print_stdout, print_stderr
import sys
import os
import subprocess
import re
import shutil
import tempfile
import time
import grp,pwd
import errno
from multiprocessing import Queue,Process

BACKUP_DEBUG = False

HEADER_FILENAME = 'backup-header'
DEFAULT_CRYPTO_ALGORITHM = 'aes-256-cbc'
DEFAULT_HMAC_ALGORITHM = 'SHA1'
# Maximum size of error message get from process stderr (including VM process)
MAX_STDERR_BYTES = 1024
# header + qubes.xml max size
HEADER_QUBES_XML_MAX_SIZE = 1024 * 1024

class BackupHeader:
    encrypted = 'encrypted'
    compressed = 'compressed'
    crypto_algorithm = 'crypto-algorithm'
    hmac_algorithm = 'hmac-algorithm'
    bool_options = ['encrypted', 'compressed']

def get_disk_usage(file_or_dir):
    if not os.path.exists(file_or_dir):
        return 0

    p = subprocess.Popen (["du", "-s", "--block-size=1", file_or_dir],
            stdout=subprocess.PIPE)
    result = p.communicate()
    m = re.match(r"^(\d+)\s.*", result[0])
    sz = int(m.group(1)) if m is not None else 0
    return sz


def file_to_backup (file_path, subdir = None):
    sz = get_disk_usage (file_path)

    if subdir is None:
        abs_file_path = os.path.abspath (file_path)
        abs_base_dir = os.path.abspath (system_path["qubes_base_dir"]) + '/'
        abs_file_dir = os.path.dirname (abs_file_path) + '/'
        (nothing, dir, subdir) = abs_file_dir.partition (abs_base_dir)
        assert nothing == ""
        assert dir == abs_base_dir
    else:
        if len(subdir) > 0 and not subdir.endswith('/'):
            subdir += '/'
    return [ { "path" : file_path, "size": sz, "subdir": subdir} ]

def backup_prepare(vms_list = None, exclude_list = None,
        print_callback = print_stdout, hide_vm_names=True):
    """If vms = None, include all (sensible) VMs; exclude_list is always applied"""
    files_to_backup = file_to_backup (system_path["qubes_store_filename"])

    if exclude_list is None:
        exclude_list = []

    qvm_collection = QubesVmCollection()
    qvm_collection.lock_db_for_writing()
    qvm_collection.load()

    if vms_list is None:
        all_vms = [vm for vm in qvm_collection.values()]
        selected_vms = [vm for vm in all_vms if vm.include_in_backups]
        appvms_to_backup = [vm for vm in selected_vms if vm.is_appvm() and not vm.internal]
        netvms_to_backup = [vm for vm in selected_vms if vm.is_netvm() and not vm.qid == 0]
        template_vms_worth_backingup = [vm for vm in selected_vms if (vm.is_template() and not vm.installed_by_rpm)]
        dom0 = [ qvm_collection[0] ]

        vms_list = appvms_to_backup + netvms_to_backup + template_vms_worth_backingup + dom0

    vms_for_backup = vms_list
    # Apply exclude list
    if exclude_list:
        vms_for_backup = [vm for vm in vms_list if vm.name not in exclude_list]

    no_vms = len (vms_for_backup)

    there_are_running_vms = False

    fields_to_display = [
        { "name": "VM", "width": 16},
        { "name": "type","width": 12 },
        { "name": "size", "width": 12}
    ]

    # Display the header
    s = ""
    for f in fields_to_display:
        fmt="{{0:-^{0}}}-+".format(f["width"] + 1)
        s += fmt.format('-')
    print_callback(s)
    s = ""
    for f in fields_to_display:
        fmt="{{0:>{0}}} |".format(f["width"] + 1)
        s += fmt.format(f["name"])
    print_callback(s)
    s = ""
    for f in fields_to_display:
        fmt="{{0:-^{0}}}-+".format(f["width"] + 1)
        s += fmt.format('-')
    print_callback(s)

    for vm in vms_for_backup:
        if vm.is_template():
            # handle templates later
            continue
        if vm.qid == 0:
            # handle dom0 later
            continue

        if hide_vm_names:
            subdir = 'vm%d/' % vm.qid
        else:
            subdir = None

        if vm.private_img is not None:
            files_to_backup += file_to_backup(vm.private_img, subdir)

        if vm.is_appvm():
            files_to_backup += file_to_backup(vm.icon_path, subdir)
        if vm.updateable:
            if os.path.exists(vm.dir_path + "/apps.templates"):
                # template
                files_to_backup += file_to_backup(vm.dir_path + "/apps.templates", subdir)
            else:
                # standaloneVM
                files_to_backup += file_to_backup(vm.dir_path + "/apps", subdir)

            if os.path.exists(vm.dir_path + "/kernels"):
                files_to_backup += file_to_backup(vm.dir_path + "/kernels", subdir)
        if os.path.exists (vm.firewall_conf):
            files_to_backup += file_to_backup(vm.firewall_conf, subdir)
        if 'appmenus_whitelist' in vm_files and \
                os.path.exists(os.path.join(vm.dir_path, vm_files['appmenus_whitelist'])):
            files_to_backup += file_to_backup(
                    os.path.join(vm.dir_path, vm_files['appmenus_whitelist']),
                    subdir)

        if vm.updateable:
            files_to_backup += file_to_backup(vm.root_img, subdir)

        s = ""
        fmt="{{0:>{0}}} |".format(fields_to_display[0]["width"] + 1)
        s += fmt.format(vm.name)

        fmt="{{0:>{0}}} |".format(fields_to_display[1]["width"] + 1)
        if vm.is_netvm():
            s += fmt.format("NetVM" + (" + Sys" if vm.updateable else ""))
        else:
            s += fmt.format("AppVM" + (" + Sys" if vm.updateable else ""))

        fmt="{{0:>{0}}} |".format(fields_to_display[2]["width"] + 1)
        s += fmt.format(size_to_human(vm.get_disk_utilization()))

        if vm.is_running():
            s +=  " <-- The VM is running, please shut it down before proceeding with the backup!"
            there_are_running_vms = True

        print_callback(s)

    for vm in vms_for_backup:
        if not vm.is_template():
            # already handled
            continue
        if vm.qid == 0:
            # handle dom0 later
            continue
        vm_sz = vm.get_disk_utilization()
        if hide_vm_names:
            template_subdir = 'vm%d/' % vm.qid
        else:
            template_subdir = os.path.relpath(
                    vm.dir_path,
                    system_path["qubes_base_dir"]) + '/'
        template_to_backup = [ {
                "path": vm.dir_path + '/.',
                "size": vm_sz,
                "subdir": template_subdir } ]
        files_to_backup += template_to_backup

        s = ""
        fmt="{{0:>{0}}} |".format(fields_to_display[0]["width"] + 1)
        s += fmt.format(vm.name)

        fmt="{{0:>{0}}} |".format(fields_to_display[1]["width"] + 1)
        s += fmt.format("Template VM")

        fmt="{{0:>{0}}} |".format(fields_to_display[2]["width"] + 1)
        s += fmt.format(size_to_human(vm_sz))

        if vm.is_running():
            s +=  " <-- The VM is running, please shut it down before proceeding with the backup!"
            there_are_running_vms = True

        print_callback(s)

    # Initialize backup flag on all VMs
    vms_for_backup_qid = [vm.qid for vm in vms_for_backup]
    for vm in qvm_collection.values():
        if vm.qid == 0:
            # handle dom0 later
            continue
        vm.backup_content = False

        if vm.qid in vms_for_backup_qid:
            vm.backup_content = True
            vm.backup_size = vm.get_disk_utilization()
            if hide_vm_names:
                vm.backup_path = 'vm%d' % vm.qid
            else:
                vm.backup_path = os.path.relpath(vm.dir_path, system_path["qubes_base_dir"])

    # Dom0 user home
    if 0 in vms_for_backup_qid:
        local_user = grp.getgrnam('qubes').gr_mem[0]
        home_dir = pwd.getpwnam(local_user).pw_dir
        # Home dir should have only user-owned files, so fix it now to prevent
        # permissions problems - some root-owned files can left after
        # 'sudo bash' and similar commands
        subprocess.check_call(['sudo', 'chown', '-R', local_user, home_dir])

        home_sz = get_disk_usage(home_dir)
        home_to_backup = [ { "path" : home_dir, "size": home_sz, "subdir": 'dom0-home/'} ]
        files_to_backup += home_to_backup

        vm = qvm_collection[0]
        vm.backup_content = True
        vm.backup_size = home_sz
        vm.backup_path = os.path.join('dom0-home', os.path.basename(home_dir))

        s = ""
        fmt="{{0:>{0}}} |".format(fields_to_display[0]["width"] + 1)
        s += fmt.format('Dom0')

        fmt="{{0:>{0}}} |".format(fields_to_display[1]["width"] + 1)
        s += fmt.format("User home")

        fmt="{{0:>{0}}} |".format(fields_to_display[2]["width"] + 1)
        s += fmt.format(size_to_human(home_sz))

        print_callback(s)

    qvm_collection.save()
    # FIXME: should be after backup completed
    qvm_collection.unlock_db()

    total_backup_sz = 0
    for file in files_to_backup:
        total_backup_sz += file["size"]

    s = ""
    for f in fields_to_display:
        fmt="{{0:-^{0}}}-+".format(f["width"] + 1)
        s += fmt.format('-')
    print_callback(s)

    s = ""
    fmt="{{0:>{0}}} |".format(fields_to_display[0]["width"] + 1)
    s += fmt.format("Total size:")
    fmt="{{0:>{0}}} |".format(fields_to_display[1]["width"] + 1 + 2 + fields_to_display[2]["width"] + 1)
    s += fmt.format(size_to_human(total_backup_sz))
    print_callback(s)

    s = ""
    for f in fields_to_display:
        fmt="{{0:-^{0}}}-+".format(f["width"] + 1)
        s += fmt.format('-')
    print_callback(s)

    if (there_are_running_vms):
        raise QubesException("Please shutdown all VMs before proceeding.")

    for fileinfo in files_to_backup:
        assert len(fileinfo["subdir"]) == 0 or fileinfo["subdir"][-1] == '/', \
            "'subdir' must ends with a '/': %s" % str(fileinfo)

    return files_to_backup

class SendWorker(Process):
    def __init__(self, queue, base_dir, backup_stdout):
        super(SendWorker, self).__init__()
        self.queue = queue
        self.base_dir = base_dir
        self.backup_stdout = backup_stdout

    def run(self):
        if BACKUP_DEBUG:
            print "Started sending thread"

        if BACKUP_DEBUG:
            print "Moving to temporary dir", self.base_dir
        os.chdir(self.base_dir)

        for filename in iter(self.queue.get,None):
            if filename == "FINISHED":
                break

            if BACKUP_DEBUG:
                print "Sending file", filename
            # This tar used for sending data out need to be as simple, as
            # simple, as featureless as possible. It will not be
            # verified before untaring.
            tar_final_cmd = ["tar", "-cO", "--posix",
                "-C", self.base_dir, filename]
            final_proc  = subprocess.Popen (tar_final_cmd,
                    stdin=subprocess.PIPE, stdout=self.backup_stdout)
            if final_proc.wait() >= 2:
                # handle only exit code 2 (tar fatal error) or greater (call failed?)
                raise QubesException("ERROR: Failed to write the backup, out of disk space? "
                        "Check console output or ~/.xsession-errors for details.")

            # Delete the file as we don't need it anymore
            if BACKUP_DEBUG:
                print "Removing file", filename
            os.remove(filename)

        if BACKUP_DEBUG:
            print "Finished sending thread"

def prepare_backup_header(target_directory, passphrase, compressed=False,
                          encrypted=False,
                          hmac_algorithm=DEFAULT_HMAC_ALGORITHM,
                          crypto_algorithm=DEFAULT_CRYPTO_ALGORITHM):
    header_file_path = os.path.join(target_directory, HEADER_FILENAME)
    with open(header_file_path, "w") as f:
        f.write("%s=%s\n" % (BackupHeader.hmac_algorithm, hmac_algorithm))
        f.write("%s=%s\n" % (BackupHeader.crypto_algorithm, crypto_algorithm))
        f.write("%s=%s\n" % (BackupHeader.encrypted, str(encrypted)))
        f.write("%s=%s\n" % (BackupHeader.compressed, str(compressed)))

    hmac = subprocess.Popen (["openssl", "dgst",
                              "-" + hmac_algorithm, "-hmac", passphrase],
                             stdin=open(header_file_path, "r"),
                             stdout=open(header_file_path + ".hmac", "w"))
    if hmac.wait() != 0:
        raise QubesException("Failed to compute hmac of header file")
    return (HEADER_FILENAME, HEADER_FILENAME+".hmac")

def backup_do(base_backup_dir, files_to_backup, passphrase,
        progress_callback = None, encrypted=False, appvm=None,
        compressed=False, hmac_algorithm=DEFAULT_HMAC_ALGORITHM,
        crypto_algorithm=DEFAULT_CRYPTO_ALGORITHM):
    total_backup_sz = 0
    for file in files_to_backup:
        total_backup_sz += file["size"]

    if compressed and encrypted:
        raise QubesException("Compressed and encrypted backups are not "
                             "supported (yet).")

    vmproc = None
    if appvm != None:
        # Prepare the backup target (Qubes service call)
        backup_target = "QUBESRPC qubes.Backup dom0"

        # If APPVM, STDOUT is a PIPE
        vmproc = appvm.run(command=backup_target, passio_popen=True,
                           passio_stderr=True)
        vmproc.stdin.write(base_backup_dir.
                           replace("\r", "").replace("\n", "")+"\n")
        backup_stdout = vmproc.stdin
    else:
        # Prepare the backup target (local file)
        backup_target = base_backup_dir + "/qubes-{0}". \
            format(time.strftime("%Y-%m-%dT%H%M%S"))

        # Create the target directory
        if not os.path.exists (base_backup_dir):
            raise QubesException(
                "ERROR: the backup directory {0} does not exists".
                format(base_backup_dir))

        # If not APPVM, STDOUT is a local file
        backup_stdout = open(backup_target,'wb')

    global blocks_backedup
    blocks_backedup = 0
    progress = blocks_backedup * 11 / total_backup_sz
    progress_callback(progress)

    feedback_file = tempfile.NamedTemporaryFile()
    backup_tmpdir = tempfile.mkdtemp(prefix="/var/tmp/backup_")

    # Tar with tapelength does not deals well with stdout (close stdout between
    # two tapes)
    # For this reason, we will use named pipes instead
    if BACKUP_DEBUG:
        print "Working in", backup_tmpdir

    backup_pipe = os.path.join(backup_tmpdir, "backup_pipe")
    if BACKUP_DEBUG:
        print "Creating pipe in:", backup_pipe
    os.mkfifo(backup_pipe)

    if BACKUP_DEBUG:
        print "Will backup:", files_to_backup

    header_files = prepare_backup_header(backup_tmpdir, passphrase,
                                         compressed=compressed,
                                         encrypted=encrypted,
                                         hmac_algorithm=hmac_algorithm,
                                         crypto_algorithm=crypto_algorithm)

    # Setup worker to send encrypted data chunks to the backup_target
    def compute_progress(new_size, total_backup_sz):
        global blocks_backedup
        blocks_backedup += new_size
        progress = blocks_backedup / float(total_backup_sz)
        progress_callback(int(round(progress*100,2)))

    to_send = Queue(10)
    send_proc = SendWorker(to_send, backup_tmpdir, backup_stdout)
    send_proc.start()

    for f in header_files:
        to_send.put(f)

    for filename in files_to_backup:
        if BACKUP_DEBUG:
            print "Backing up", filename

        backup_tempfile = os.path.join(backup_tmpdir,
                                       filename["subdir"],
                                       os.path.basename(filename["path"]))
        if BACKUP_DEBUG:
            print "Using temporary location:", backup_tempfile

        # Ensure the temporary directory exists
        if not os.path.isdir(os.path.dirname(backup_tempfile)):
            os.makedirs(os.path.dirname(backup_tempfile))

        # The first tar cmd can use any complex feature as we want. Files will
        # be verified before untaring this.
        # Prefix the path in archive with filename["subdir"] to have it verified during untar
        tar_cmdline = ["tar", "-Pc", '--sparse',
                       "-f", backup_pipe,
                       '--tape-length', str(100000),
                       '-C', os.path.dirname(filename["path"]),
                       '--xform', 's:^[^/]:%s\\0:' % filename["subdir"],
                       os.path.basename(filename["path"])
                       ]

        if BACKUP_DEBUG:
            print " ".join(tar_cmdline)

        # Tips: Popen(bufsize=0)
        # Pipe: tar-sparse | encryptor [| hmac] | tar | backup_target
        # Pipe: tar-sparse [| hmac] | tar | backup_target
        tar_sparse = subprocess.Popen (tar_cmdline, stdin=subprocess.PIPE,
                stderr=(open(os.devnull, 'w') if not BACKUP_DEBUG else None))

        # Wait for compressor (tar) process to finish or for any error of other
        # subprocesses
        i = 0
        run_error = "paused"
        while run_error == "paused":
            pipe = open(backup_pipe,'rb')

            # Start HMAC
            hmac = subprocess.Popen (["openssl", "dgst",
                                      "-" + hmac_algorithm, "-hmac", passphrase],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)

            # Prepare a first chunk
            chunkfile = backup_tempfile + "." + "%03d" % i
            i += 1
            chunkfile_p = open(chunkfile,'wb')

            common_args = {
                        'backup_target': chunkfile_p,
                        'total_backup_sz': total_backup_sz,
                        'hmac': hmac,
                        'vmproc': vmproc,
                        'addproc': tar_sparse,
                        'progress_callback': compute_progress,
            }
            if encrypted:
                # Start encrypt
                # If no cipher is provided, the data is forwarded unencrypted !!!
                encryptor = subprocess.Popen (["openssl", "enc",
                        "-e", "-" + crypto_algorithm,
                        "-pass", "pass:"+passphrase] +
                        (["-z"] if compressed else []),
                        stdin=pipe, stdout=subprocess.PIPE)
                run_error = wait_backup_feedback(
                        in_stream=encryptor.stdout, streamproc=encryptor,
                        **common_args)
            elif compressed:
                compressor = subprocess.Popen (["gzip"],
                        stdin=pipe, stdout=subprocess.PIPE)
                run_error = wait_backup_feedback(
                        in_stream=compressor.stdout, streamproc=compressor,
                        **common_args)
            else:
                run_error = wait_backup_feedback(
                        in_stream=pipe, streamproc=None,
                        **common_args)

            chunkfile_p.close()

            if BACKUP_DEBUG:
                print "Wait_backup_feedback returned:", run_error

            if len(run_error) > 0:
                send_proc.terminate()
                if run_error == "VM" and vmproc:
                    raise QubesException("Failed to write the backup, VM output:\n" +
                            vmproc.stderr.read(MAX_STDERR_BYTES))
                else:
                    raise QubesException("Failed to perform backup: error in "+ \
                            run_error)

            # Send the chunk to the backup target
            to_send.put(os.path.relpath(chunkfile, backup_tmpdir))

            # Close HMAC
            hmac.stdin.close()
            hmac.wait()
            if BACKUP_DEBUG:
                print "HMAC proc return code:", hmac.poll()

            # Write HMAC data next to the chunk file
            hmac_data = hmac.stdout.read()
            if BACKUP_DEBUG:
                print "Writing hmac to", chunkfile+".hmac"
            hmac_file = open(chunkfile+".hmac",'w')
            hmac_file.write(hmac_data)
            hmac_file.flush()
            hmac_file.close()

            pipe.close()

            # Send the HMAC to the backup target
            to_send.put(os.path.relpath(chunkfile, backup_tmpdir)+".hmac")

            if tar_sparse.poll() is None:
                # Release the next chunk
                if BACKUP_DEBUG:
                    print "Release next chunk for process:", tar_sparse.poll()
                #tar_sparse.stdout = subprocess.PIPE
                tar_sparse.stdin.write("\n")
                tar_sparse.stdin.flush()
                run_error="paused"
            else:
                if BACKUP_DEBUG:
                    print "Finished tar sparse with error", tar_sparse.poll()

    to_send.put("FINISHED")
    send_proc.join()

    if send_proc.exitcode != 0:
        raise QubesException("Failed to send backup: error in the sending process")

    if vmproc:
        if BACKUP_DEBUG:
            print "VMProc1 proc return code:", vmproc.poll()
            print "Sparse1 proc return code:", tar_sparse.poll()
        vmproc.stdin.close()

    shutil.rmtree(backup_tmpdir)

'''
' Wait for backup chunk to finish
' - Monitor all the processes (streamproc, hmac, vmproc, addproc) for errors
' - Copy stdout of streamproc to backup_target and hmac stdin if available
' - Compute progress based on total_backup_sz and send progress to
'   progress_callback function
' - Returns if
' -     one of the monitored processes error out (streamproc, hmac, vmproc,
'       addproc), along with the processe that failed
' -     all of the monitored processes except vmproc finished successfully
'       (vmproc termination is controlled by the python script)
' -     streamproc does not delivers any data anymore (return with the error
'       "")
'''
def wait_backup_feedback(progress_callback, in_stream, streamproc,
        backup_target, total_backup_sz, hmac=None, vmproc=None, addproc=None,
        remove_trailing_bytes=0):

    buffer_size = 409600

    run_error = None
    run_count = 1
    blocks_backedup = 0
    while run_count > 0 and run_error == None:

        buffer = in_stream.read(buffer_size)
        progress_callback(len(buffer), total_backup_sz)

        run_count = 0
        if hmac:
            retcode=hmac.poll()
            if retcode != None:
                if retcode != 0:
                    run_error = "hmac"
            else:
                run_count += 1

        if addproc:
            retcode=addproc.poll()
            if retcode != None:
                if retcode != 0:
                    run_error = "addproc"
            else:
                run_count += 1

        if vmproc:
            retcode = vmproc.poll()
            if retcode != None:
                if retcode != 0:
                    run_error = "VM"
                    if BACKUP_DEBUG:
                        print vmproc.stdout.read()
            else:
                # VM should run until the end
                pass

        if streamproc:
            retcode=streamproc.poll()
            if retcode != None:
                if retcode != 0:
                    run_error = "streamproc"
                    break
                elif retcode == 0 and len(buffer) <= 0:
                    return ""
            run_count += 1

        else:
            if len(buffer) <= 0:
                return ""

        backup_target.write(buffer)

        if hmac:
            hmac.stdin.write(buffer)

    return run_error

def verify_hmac(filename, hmacfile, passphrase, algorithm):
    if BACKUP_DEBUG:
        print "Verifying file "+filename

    if hmacfile != filename + ".hmac":
        raise QubesException(
            "ERROR: expected hmac for {}, but got {}".\
            format(filename, hmacfile))

    hmac_proc = subprocess.Popen (["openssl", "dgst", "-" + algorithm,
                                   "-hmac", passphrase],
            stdin=open(filename,'rb'),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    hmac_stdout, hmac_stderr = hmac_proc.communicate()

    if len(hmac_stderr) > 0:
        raise QubesException("ERROR: verify file {0}: {1}".format(filename, hmac_stderr))
    else:
        if BACKUP_DEBUG:
            print "Loading hmac for file " + filename
        hmac = load_hmac(open(hmacfile,'r').read())

        if len(hmac) > 0 and load_hmac(hmac_stdout) == hmac:
            os.unlink(hmacfile)
            if BACKUP_DEBUG:
                print "File verification OK -> Sending file " + filename
            return True
        else:
            raise QubesException(
                    "ERROR: invalid hmac for file {0}: {1}. " \
                    "Is the passphrase correct?".\
                    format(filename, load_hmac(hmac_stdout)))
    # Not reachable
    return False


class ExtractWorker(Process):
    def __init__(self, queue, base_dir, passphrase, encrypted, total_size,
            print_callback, error_callback, progress_callback, vmproc=None,
            compressed = False, crypto_algorithm=DEFAULT_CRYPTO_ALGORITHM):
        super(ExtractWorker, self).__init__()
        self.queue = queue
        self.base_dir = base_dir
        self.passphrase = passphrase
        self.encrypted = encrypted
        self.compressed = compressed
        self.crypto_algorithm = crypto_algorithm
        self.total_size = total_size
        self.blocks_backedup = 0
        self.tar2_process = None
        self.tar2_current_file = None
        self.decompressor_process = None
        self.decryptor_process = None

        self.print_callback = print_callback
        self.error_callback = error_callback
        self.progress_callback = progress_callback

        self.vmproc = vmproc

        self.restore_pipe = os.path.join(self.base_dir,"restore_pipe")
        if BACKUP_DEBUG:
            print "Creating pipe in:", self.restore_pipe
        os.mkfifo(self.restore_pipe)

    def compute_progress(self, new_size, total_size):
        if self.progress_callback:
            self.blocks_backedup += new_size
            progress = self.blocks_backedup / float(self.total_size)
            progress = int(round(progress*100,2))
            self.progress_callback(progress)

    def run(self):
        try:
            self.__run__()
        except Exception as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            # Cleanup children
            for process in [self.decompressor_process,
                    self.decryptor_process,
                    self.tar2_process]:
                if process:
                    # FIXME: kill()?
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    process.wait()
            self.error_callback(str(e))
            raise e, None, exc_traceback

    def __run__(self):
        if BACKUP_DEBUG:
            self.print_callback("Started sending thread")
            self.print_callback("Moving to dir "+self.base_dir)
        os.chdir(self.base_dir)

        filename = None

        for filename in iter(self.queue.get, None):
            if filename == "FINISHED" or filename == "ERROR":
                break

            if BACKUP_DEBUG:
                self.print_callback("Extracting file "+filename)

            if filename.endswith('.000'):
                # next file
                if self.tar2_process != None:
                    if self.tar2_process.wait() != 0:
                        raise QubesException(
                                "ERROR: unable to extract files for {0}.".\
                                format(self.tar2_current_file))
                    else:
                        # Finished extracting the tar file
                        self.tar2_process = None
                        self.tar2_current_file = None

                tar2_cmdline = ['tar',
                    '-xMk%sf' % ("v" if BACKUP_DEBUG else ""), self.restore_pipe,
                    os.path.relpath(filename.rstrip('.000'))]
                if BACKUP_DEBUG:
                    self.print_callback("Running command "+str(tar2_cmdline))
                self.tar2_process = subprocess.Popen(tar2_cmdline,
                        stdin=subprocess.PIPE,
                        stderr=(None if BACKUP_DEBUG else open('/dev/null', 'w')))
            else:
                if BACKUP_DEBUG:
                    self.print_callback("Releasing next chunck")
                self.tar2_process.stdin.write("\n")
                self.tar2_process.stdin.flush()
            self.tar2_current_file = filename

            pipe = open(self.restore_pipe,'wb')
            common_args = {
                        'backup_target': pipe,
                        'total_backup_sz': self.total_size,
                        'hmac': None,
                        'vmproc': self.vmproc,
                        'addproc': self.tar2_process
            }
            if self.encrypted:
                # Start decrypt
                self.decryptor_process = subprocess.Popen (["openssl", "enc",
                        "-d", "-" + self.crypto_algorithm,
                        "-pass", "pass:"+self.passphrase] +
                        (["-z"] if self.compressed else []),
                        stdin=open(filename,'rb'),
                        stdout=subprocess.PIPE)

                run_error = wait_backup_feedback(
                        progress_callback=self.compute_progress,
                        in_stream=self.decryptor_process.stdout,
                        streamproc=self.decryptor_process,
                        **common_args)
            elif self.compressed:
                self.decompressor_process = subprocess.Popen (["gzip", "-d"],
                        stdin=open(filename,'rb'),
                        stdout=subprocess.PIPE)

                run_error = wait_backup_feedback(
                        progress_callback=self.compute_progress,
                        in_stream=self.decompressor_process.stdout,
                        streamproc=self.decompressor_process,
                        **common_args)
            else:
                run_error = wait_backup_feedback(
                        progress_callback=self.compute_progress,
                        in_stream=open(filename,"rb"), streamproc=None,
                        **common_args)

            try:
                pipe.close()
            except IOError as e:
                if e.errno == errno.EPIPE:
                    if BACKUP_DEBUG:
                        self.error_callback("Got EPIPE while closing pipe to the inner tar process")
                    # ignore the error
                else:
                    raise
            if len(run_error):
                raise QubesException("Error while processing '%s': %s failed" % \
                        (self.tar2_current_file, run_error))

            # Delete the file as we don't need it anymore
            if BACKUP_DEBUG:
                self.print_callback("Removing file "+filename)
            os.remove(filename)

        os.unlink(self.restore_pipe)

        if self.tar2_process is not None:
            if filename == "ERROR":
                self.tar2_process.terminate()
            if self.tar2_process.wait() != 0:
                raise QubesException(
                    "ERROR: unable to extract files for {0}.{1}".
                    format(self.tar2_current_file,
                           (" Perhaps the backup is encrypted?"
                            if not self.encrypted else "")))
            else:
                # Finished extracting the tar file
                self.tar2_process = None

        if BACKUP_DEBUG:
            self.print_callback("Finished extracting thread")


def get_supported_hmac_algo(hmac_algorithm):
    # Start with provided default
    if hmac_algorithm:
        yield hmac_algorithm
    proc = subprocess.Popen(['openssl', 'list-message-digest-algorithms'],
                            stdout=subprocess.PIPE)
    for algo in proc.stdout.readlines():
        if '=>' in algo:
            continue
        yield algo.strip()
    proc.wait()

def parse_backup_header(filename):
    header_data = {}
    with open(filename, 'r') as f:
        for line in f.readlines():
            if line.count('=') != 1:
                raise QubesException("Invalid backup header (line %s)" % line)
            (key, value) = line.strip().split('=')
            if not any([key == getattr(BackupHeader, attr) for attr in dir(
                    BackupHeader)]):
                # Ignoring unknown option
                continue
            if key in BackupHeader.bool_options:
                value = value.lower() in ["1", "true", "yes"]
            header_data[key] = value
    return header_data

def restore_vm_dirs (backup_source, restore_tmpdir, passphrase, vms_dirs, vms,
        vms_size, print_callback=None, error_callback=None,
        progress_callback=None, encrypted=False, appvm=None,
        compressed = False, hmac_algorithm=DEFAULT_HMAC_ALGORITHM,
        crypto_algorithm=DEFAULT_CRYPTO_ALGORITHM):

    if BACKUP_DEBUG:
        print_callback("Working in temporary dir:"+restore_tmpdir)
    print_callback("Extracting data: " + size_to_human(vms_size)+" to restore")

    header_data = None
    vmproc = None
    if appvm != None:
        # Prepare the backup target (Qubes service call)
        backup_target = "QUBESRPC qubes.Restore dom0"

        # If APPVM, STDOUT is a PIPE
        vmproc = appvm.run(command = backup_target, passio_popen = True, passio_stderr=True)
        vmproc.stdin.write(backup_source.replace("\r","").replace("\n","")+"\n")

        # Send to tar2qfile the VMs that should be extracted
        vmproc.stdin.write(" ".join(vms_dirs)+"\n")

        backup_stdin = vmproc.stdout
        tar1_command = ['/usr/libexec/qubes/qfile-dom0-unpacker',
            str(os.getuid()), restore_tmpdir, '-v']
    else:
        backup_stdin = open(backup_source,'rb')

        tar1_command = ['tar',
            '-ixvf', backup_source,
            '-C', restore_tmpdir] + vms_dirs

    tar1_env = os.environ.copy()
    # TODO: add some safety margin?
    tar1_env['UPDATES_MAX_BYTES'] = str(vms_size)
    # Restoring only header
    if vms_dirs and vms_dirs[0] == HEADER_FILENAME:
        # backup-header, backup-header.hmac, qubes-xml.000, qubes-xml.000.hmac
        tar1_env['UPDATES_MAX_FILES'] = '4'
    else:
        # Currently each VM consists of at most 7 archives (count
        # file_to_backup calls in backup_prepare()), but add some safety
        # margin for further extensions. Each archive is divided into 100MB
        # chunks. Additionally each file have own hmac file. So assume upper
        # limit as 2*(10*COUNT_OF_VMS+TOTAL_SIZE/100MB)
        tar1_env['UPDATES_MAX_FILES'] = str(2*(10*len(vms_dirs) +
                                               int(vms_size/(100*1024*1024))))
    if BACKUP_DEBUG:
        print_callback("Run command"+str(tar1_command))
    command = subprocess.Popen(tar1_command,
            stdin=backup_stdin,
            stdout=vmproc.stdin if vmproc else subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=tar1_env)

    # qfile-dom0-unpacker output filelist on stderr (and have stdout connected
    # to the VM), while tar output filelist on stdout
    if appvm:
        filelist_pipe = command.stderr
    else:
        filelist_pipe = command.stdout

    expect_tar_error = False

    to_extract = Queue()
    nextfile = None

    # If want to analyze backup header, do it now
    if vms_dirs and vms_dirs[0] == HEADER_FILENAME:
        filename = filelist_pipe.readline().strip()
        hmacfile = filelist_pipe.readline().strip()
        nextfile = filelist_pipe.readline().strip()

        if BACKUP_DEBUG:
            print_callback("Got backup header and hmac: %s, %s" % (filename,
                                                                   hmacfile))

        if not filename or filename=="EOF" or \
                not hmacfile or hmacfile == "EOF":
            if appvm:
                vmproc.wait()
                proc_error_msg = vmproc.stderr.read(MAX_STDERR_BYTES)
            else:
                command.wait()
                proc_error_msg = command.stderr.read(MAX_STDERR_BYTES)
            raise QubesException("Premature end of archive while receiving "
                                 "backup header. Process output:\n" +
                                 proc_error_msg)
        filename = os.path.join(restore_tmpdir, filename)
        hmacfile = os.path.join(restore_tmpdir, hmacfile)
        file_ok = False
        for hmac_algo in get_supported_hmac_algo(hmac_algorithm):
            try:
                if verify_hmac(filename, hmacfile, passphrase, hmac_algo):
                    file_ok = True
                    hmac_algorithm = hmac_algo
                    break
            except QubesException:
                # Ignore exception here, try the next algo
                pass
        if not file_ok:
            raise QubesException("Corrupted backup header (hmac verification "
                                 "failed)")
        if os.path.basename(filename) == HEADER_FILENAME:
            header_data = parse_backup_header(filename)
            if BackupHeader.crypto_algorithm in header_data:
                crypto_algorithm = header_data[BackupHeader.crypto_algorithm]
            if BackupHeader.hmac_algorithm in header_data:
                hmac_algorithm = header_data[BackupHeader.hmac_algorithm]
            if BackupHeader.compressed in header_data:
                compressed = header_data[BackupHeader.compressed]
            if BackupHeader.encrypted in header_data:
                encrypted = header_data[BackupHeader.encrypted]
            os.unlink(filename)
        else:
            # If this isn't backup header, pass it to ExtractWorker
            to_extract.put(filename)
            # when tar do not find expected file in archive, it exit with
            # code 2. This will happen because we've requested backup-header
            # file, but the archive do not contain it. Ignore this particular
            # error.
            if not appvm:
                expect_tar_error = True

    # Setup worker to extract encrypted data chunks to the restore dirs
    # Create the process here to pass it options extracted from backup header
    extract_proc = ExtractWorker(queue=to_extract,
            base_dir=restore_tmpdir,
            passphrase=passphrase,
            encrypted=encrypted,
            compressed=compressed,
            crypto_algorithm = crypto_algorithm,
            total_size=vms_size,
            print_callback=print_callback,
            error_callback=error_callback,
            progress_callback=progress_callback)
    extract_proc.start()


    try:
        filename = None
        while True:
            if nextfile is not None:
                filename = nextfile
            else:
                filename = filelist_pipe.readline().strip()

            if BACKUP_DEBUG:
                print_callback("Getting new file:"+filename)

            if not filename or filename=="EOF":
                break

            hmacfile = filelist_pipe.readline().strip()
            # if reading archive directly with tar, wait for next filename -
            # tar prints filename before processing it, so wait for the next one to be
            # sure that whole file was extracted
            if not appvm:
                nextfile = filelist_pipe.readline().strip()

            if BACKUP_DEBUG:
                print_callback("Getting hmac:"+hmacfile)
            if not hmacfile or hmacfile=="EOF":
                # Premature end of archive, either of tar1_command or vmproc exited with error
                break

            if not any(map(lambda x: filename.startswith(x), vms_dirs)):
                if BACKUP_DEBUG:
                    print_callback("Ignoring VM not selected for restore")
                os.unlink(os.path.join(restore_tmpdir, filename))
                os.unlink(os.path.join(restore_tmpdir, hmacfile))
                continue

            if verify_hmac(os.path.join(restore_tmpdir,filename),
                    os.path.join(restore_tmpdir,hmacfile),
                    passphrase, hmac_algorithm):
                to_extract.put(os.path.join(restore_tmpdir, filename))

        if command.wait() != 0 and not expect_tar_error:
            raise QubesException(
                    "ERROR: unable to read the qubes backup file {0} ({1}). " \
                    "Is it really a backup?".format(backup_source, command.wait()))
        if vmproc:
            if vmproc.wait() != 0:
                raise QubesException(
                        "ERROR: unable to read the qubes backup {0} " \
                        "because of a VM error: {1}".format(
                            backup_source, vmproc.stderr.read(MAX_STDERR_BYTES)))

        if filename and filename!="EOF":
            raise QubesException("Premature end of archive, the last file was %s" % filename)
    except:
        to_extract.put("ERROR")
        extract_proc.join()
        raise
    else:
        to_extract.put("FINISHED")

    if BACKUP_DEBUG:
        print_callback("Waiting for the extraction process to finish...")
    extract_proc.join()
    if BACKUP_DEBUG:
        print_callback("Extraction process finished with code:" + \
                str(extract_proc.exitcode))
    if extract_proc.exitcode != 0:
        raise QubesException(
                "ERROR: unable to extract the qubes backup. " \
                "Check extracting process errors.")

    return header_data

def backup_restore_set_defaults(options):
    if 'use-default-netvm' not in options:
        options['use-default-netvm'] = False
    if 'use-none-netvm' not in options:
        options['use-none-netvm'] = False
    if 'use-default-template' not in options:
        options['use-default-template'] = False
    if 'dom0-home' not in options:
        options['dom0-home'] = True
    if 'replace-template' not in options:
        options['replace-template'] = []

    return options

def load_hmac(hmac):
    hmac = hmac.strip().split("=")
    if len(hmac) > 1:
        hmac = hmac[1].strip()
    else:
        raise QubesException("ERROR: invalid hmac file content")

    return hmac

def backup_detect_format_version(backup_location):
    if os.path.exists(os.path.join(backup_location, 'qubes.xml')):
        return 1
    else:
        return 2

def backup_restore_header(source, passphrase,
        print_callback = print_stdout, error_callback = print_stderr,
        encrypted=False, appvm=None, compressed = False, format_version = None,
        hmac_algorithm = DEFAULT_HMAC_ALGORITHM,
        crypto_algorithm = DEFAULT_CRYPTO_ALGORITHM):

    vmproc = None

    feedback_file = tempfile.NamedTemporaryFile()
    restore_tmpdir = tempfile.mkdtemp(prefix="/var/tmp/restore_")

    if format_version == None:
        format_version = backup_detect_format_version(source)

    if format_version == 1:
        return (restore_tmpdir, os.path.join(source, 'qubes.xml'), None)

    # tar2qfile matches only beginnings, while tar full path
    if appvm:
        extract_filter = [HEADER_FILENAME, 'qubes.xml.000']
    else:
        extract_filter = [HEADER_FILENAME, HEADER_FILENAME+'.hmac',
                          'qubes.xml.000', 'qubes.xml.000.hmac']

    header_data = restore_vm_dirs (source,
            restore_tmpdir,
            passphrase=passphrase,
            vms_dirs=extract_filter,
            vms=None,
            vms_size=HEADER_QUBES_XML_MAX_SIZE,
            hmac_algorithm=hmac_algorithm,
            crypto_algorithm=crypto_algorithm,
            print_callback=print_callback,
            error_callback=error_callback,
            progress_callback=None,
            encrypted=encrypted,
            compressed=compressed,
            appvm=appvm)

    return (restore_tmpdir, os.path.join(restore_tmpdir, "qubes.xml"),
            header_data)

def restore_info_verify(restore_info, host_collection):
    options = restore_info['$OPTIONS$']
    for vm in restore_info.keys():
        if vm in ['$OPTIONS$', 'dom0']:
            continue

        vm_info = restore_info[vm]

        vm_info.pop('excluded', None)
        if 'exclude' in options.keys():
            if vm in options['exclude']:
                vm_info['excluded'] = True

        vm_info.pop('already-exists', None)
        if host_collection.get_vm_by_name (vm) is not None:
            vm_info['already-exists'] = True

        # check template
        vm_info.pop('missing-template', None)
        if vm_info['template']:
            template_name = vm_info['template']
            host_template = host_collection.get_vm_by_name(template_name)
            if not host_template or not host_template.is_template():
                # Maybe the (custom) template is in the backup?
                if not (template_name in restore_info.keys() and
                            restore_info[template_name]['vm'].is_template()):
                    if options['use-default-template']:
                        if 'orig-template' not in vm_info.keys():
                            vm_info['orig-template'] = template_name
                        vm_info['template'] = host_collection\
                            .get_default_template().name
                    else:
                        vm_info['missing-template'] = True

        # check netvm
        vm_info.pop('missing-netvm', None)
        if vm_info['netvm']:
            netvm_name = vm_info['netvm']

            netvm_on_host = host_collection.get_vm_by_name (netvm_name)

            # No netvm on the host?
            if not ((netvm_on_host is not None) and netvm_on_host.is_netvm()):

                # Maybe the (custom) netvm is in the backup?
                if not (netvm_name in restore_info.keys() and \
                        restore_info[netvm_name]['vm'].is_netvm()):
                    if options['use-default-netvm']:
                        vm_info['netvm'] = host_collection\
                            .get_default_netvm().name
                        vm_info['vm'].uses_default_netvm = True
                    elif options['use-none-netvm']:
                        vm_info['netvm'] = None
                    else:
                        vm_info['missing-netvm'] = True

        vm_info['good-to-go'] = not any([(prop in vm_info.keys()) for
                                         prop in ['missing-netvm',
                                                  'missing-template',
                                                  'already-exists',
                                                  'excluded']])

    return restore_info

def backup_restore_prepare(backup_location, passphrase, options = {},
        host_collection = None, encrypted=False, appvm=None,
        compressed = False, print_callback = print_stdout, error_callback = print_stderr,
        format_version=None, hmac_algorithm=DEFAULT_HMAC_ALGORITHM,
        crypto_algorithm=DEFAULT_CRYPTO_ALGORITHM):
    # Defaults
    backup_restore_set_defaults(options)

    #### Private functions begin
    def is_vm_included_in_backup_v1 (backup_dir, vm):
        if vm.qid == 0:
            return os.path.exists(os.path.join(backup_dir,'dom0-home'))

        backup_vm_dir_path = vm.dir_path.replace (system_path["qubes_base_dir"], backup_dir)

        if os.path.exists (backup_vm_dir_path):
            return True
        else:
            return False
    def is_vm_included_in_backup_v2 (backup_dir, vm):
        if vm.backup_content:
            return True
        else:
            return False

    def find_template_name(template, replaces):
        rx_replace = re.compile("(.*):(.*)")
        for r in replaces:
            m = rx_replace.match(r)
            if m.group(1) == template:
                return m.group(2)

        return template
    #### Private functions end

    # Format versions:
    #  1 - Qubes R1, Qubes R2 beta1, beta2
    #  2 - Qubes R2 beta3+

    if format_version is None:
        format_version = backup_detect_format_version(backup_location)

    if format_version == 1:
        is_vm_included_in_backup = is_vm_included_in_backup_v1
    elif format_version == 2:
        is_vm_included_in_backup = is_vm_included_in_backup_v2
        if not appvm:
            if not os.path.isfile(backup_location):
                raise QubesException("Invalid backup location (not a file or "
                                     "directory with qubes.xml)"
                                     ": %s" % str(
                    backup_location))
    else:
        raise QubesException("Unknown backup format version: %s" % str(format_version))

    (restore_tmpdir, qubes_xml, header_data) = backup_restore_header(
        backup_location,
        passphrase,
        encrypted=encrypted,
        appvm=appvm,
        compressed=compressed,
        hmac_algorithm=hmac_algorithm,
        crypto_algorithm=crypto_algorithm,
        print_callback=print_callback,
        error_callback=error_callback,
        format_version=format_version)

    if header_data:
        if BackupHeader.crypto_algorithm in header_data:
            crypto_algorithm = header_data[BackupHeader.crypto_algorithm]
        if BackupHeader.hmac_algorithm in header_data:
            hmac_algorithm = header_data[BackupHeader.hmac_algorithm]
        if BackupHeader.compressed in header_data:
            compressed = header_data[BackupHeader.compressed]
        if BackupHeader.encrypted in header_data:
            encrypted = header_data[BackupHeader.encrypted]

    if BACKUP_DEBUG:
        print "Loading file", qubes_xml
    backup_collection = QubesVmCollection(store_filename = qubes_xml)
    backup_collection.lock_db_for_reading()
    backup_collection.load()

    if host_collection is None:
        host_collection = QubesVmCollection()
        host_collection.lock_db_for_reading()
        host_collection.load()
        host_collection.unlock_db()

    backup_vms_list = [vm for vm in backup_collection.values()]
    vms_to_restore = {}

    # ... and the actual data
    for vm in backup_vms_list:
        if vm.qid == 0:
            # Handle dom0 as special case later
            continue
        if is_vm_included_in_backup (backup_location, vm):
            if BACKUP_DEBUG:
                print vm.name,"is included in backup"

            vms_to_restore[vm.name] = {}
            vms_to_restore[vm.name]['vm'] = vm;

            if vm.template is None:
                vms_to_restore[vm.name]['template'] = None
            else:
                templatevm_name = find_template_name(vm.template.name, options['replace-template'])
                vms_to_restore[vm.name]['template'] = templatevm_name

            if vm.netvm is None:
                vms_to_restore[vm.name]['netvm'] = None
            else:
                netvm_name = vm.netvm.name
                vms_to_restore[vm.name]['netvm'] = netvm_name
                # Set to None to not confuse QubesVm object from backup
                # collection with host collection (further in clone_attrs). Set
                # directly _netvm to suppress setter action, especially
                # modifying firewall
                vm._netvm = None

    # Store restore parameters
    options['location'] = backup_location
    options['restore_tmpdir'] = restore_tmpdir
    options['passphrase'] = passphrase
    options['encrypted'] = encrypted
    options['compressed'] = compressed
    options['hmac_algorithm'] = hmac_algorithm
    options['crypto_algorithm'] = crypto_algorithm
    options['appvm'] = appvm
    options['format_version'] = format_version
    vms_to_restore['$OPTIONS$'] = options

    vms_to_restore = restore_info_verify(vms_to_restore, host_collection)

    # ...and dom0 home
    if options['dom0-home'] and \
            is_vm_included_in_backup(backup_location, backup_collection[0]):
        vm = backup_collection[0]
        vms_to_restore['dom0'] = {}
        if format_version == 1:
            vms_to_restore['dom0']['subdir'] = \
                os.listdir(os.path.join(backup_location, 'dom0-home'))[0]
            vms_to_restore['dom0']['size'] = 0 # unknown
        else:
            vms_to_restore['dom0']['subdir'] = vm.backup_path
            vms_to_restore['dom0']['size'] = vm.backup_size
        local_user = grp.getgrnam('qubes').gr_mem[0]

        dom0_home = vms_to_restore['dom0']['subdir']

        vms_to_restore['dom0']['username'] = os.path.basename(dom0_home)
        if vms_to_restore['dom0']['username'] != local_user:
            vms_to_restore['dom0']['username-mismatch'] = True
            if not options['ignore-dom0-username-mismatch']:
                vms_to_restore['dom0']['good-to-go'] = False

        if 'good-to-go' not in vms_to_restore['dom0']:
            vms_to_restore['dom0']['good-to-go'] = True

    # Not needed - all the data stored in vms_to_restore
    if format_version == 2:
        os.unlink(qubes_xml)
    return vms_to_restore

def backup_restore_print_summary(restore_info, print_callback = print_stdout):
    fields = {
        "qid": {"func": "vm.qid"},

        "name": {"func": "('[' if vm.is_template() else '')\
                 + ('{' if vm.is_netvm() else '')\
                 + vm.name \
                 + (']' if vm.is_template() else '')\
                 + ('}' if vm.is_netvm() else '')"},

        "type": {"func": "'Tpl' if vm.is_template() else \
                 'HVM' if vm.type == 'HVM' else \
                 vm.type.replace('VM','')"},

        "updbl" : {"func": "'Yes' if vm.updateable else ''"},

        "template": {"func": "'n/a' if vm.is_template() or vm.template is None else\
                     vm_info['template']"},

        "netvm": {"func": "'n/a' if vm.is_netvm() and not vm.is_proxyvm() else\
                  ('*' if vm.uses_default_netvm else '') +\
                    vm_info['netvm'] if vm_info['netvm'] is not None else '-'"},

        "label" : {"func" : "vm.label.name"},
    }

    fields_to_display = ["name", "type", "template", "updbl", "netvm", "label" ]

    # First calculate the maximum width of each field we want to display
    total_width = 0
    for f in fields_to_display:
        fields[f]["max_width"] = len(f)
        for vm_info in restore_info.values():
            if 'vm' in vm_info.keys():
                vm = vm_info['vm']
                l = len(str(eval(fields[f]["func"])))
                if l > fields[f]["max_width"]:
                    fields[f]["max_width"] = l
        total_width += fields[f]["max_width"]

    print_callback("")
    print_callback("The following VMs are included in the backup:")
    print_callback("")

    # Display the header
    s = ""
    for f in fields_to_display:
        fmt="{{0:-^{0}}}-+".format(fields[f]["max_width"] + 1)
        s += fmt.format('-')
    print_callback(s)
    s = ""
    for f in fields_to_display:
        fmt="{{0:>{0}}} |".format(fields[f]["max_width"] + 1)
        s += fmt.format(f)
    print_callback(s)
    s = ""
    for f in fields_to_display:
        fmt="{{0:-^{0}}}-+".format(fields[f]["max_width"] + 1)
        s += fmt.format('-')
    print_callback(s)

    for vm_info in restore_info.values():
        # Skip non-VM here
        if not 'vm' in vm_info:
            continue
        vm = vm_info['vm']
        s = ""
        for f in fields_to_display:
            fmt="{{0:>{0}}} |".format(fields[f]["max_width"] + 1)
            s += fmt.format(eval(fields[f]["func"]))

        if 'excluded' in vm_info and vm_info['excluded']:
            s += " <-- Excluded from restore"
        elif 'already-exists' in vm_info:
            s +=  " <-- A VM with the same name already exists on the host!"
        elif 'missing-template' in vm_info:
            s += " <-- No matching template on the host or in the backup found!"
        elif 'missing-netvm' in vm_info:
            s += " <-- No matching netvm on the host or in the backup found!"
        elif 'orig-template' in vm_info:
            s += " <-- Original template was '%s'" % (vm_info['orig-template'])

        print_callback(s)

    if 'dom0' in restore_info.keys():
        s = ""
        for f in fields_to_display:
            fmt="{{0:>{0}}} |".format(fields[f]["max_width"] + 1)
            if f == "name":
                s += fmt.format("Dom0")
            elif f == "type":
                s += fmt.format("Home")
            else:
                s += fmt.format("")
        if 'username-mismatch' in restore_info['dom0']:
            s += " <-- username in backup and dom0 mismatch"

        print_callback(s)

def backup_restore_do(restore_info,
        host_collection = None, print_callback = print_stdout,
        error_callback = print_stderr, progress_callback = None,
        ):

    ### Private functions begin
    def restore_vm_dir_v1 (backup_dir, src_dir, dst_dir):

        backup_src_dir = src_dir.replace (system_path["qubes_base_dir"], backup_dir)

        # We prefer to use Linux's cp, because it nicely handles sparse files
        retcode = subprocess.call (["cp", "-rp", backup_src_dir, dst_dir])
        if retcode != 0:
            raise QubesException(
                "*** Error while copying file {0} to {1}".format(backup_src_dir,
                                                                 dst_dir))
    ### Private functions end

    options = restore_info['$OPTIONS$']
    backup_location = options['location']
    restore_tmpdir = options['restore_tmpdir']
    passphrase = options['passphrase']
    encrypted = options['encrypted']
    compressed = options['compressed']
    hmac_algorithm = options['hmac_algorithm']
    crypto_algorithm = options['crypto_algorithm']
    appvm = options['appvm']
    format_version = options['format_version']

    if format_version is None:
        format_version = backup_detect_format_version(backup_location)

    lock_obtained = False
    if host_collection is None:
        host_collection = QubesVmCollection()
        host_collection.lock_db_for_writing()
        host_collection.load()
        lock_obtained = True

    # Perform VM restoration in backup order
    vms_dirs = []
    vms_size = 0
    vms = {}
    for vm_info in restore_info.values():
        if 'vm' not in vm_info:
            continue
        if not vm_info['good-to-go']:
            continue
        vm = vm_info['vm']
        if format_version == 2:
            vms_size += vm.backup_size
            vms_dirs.append(vm.backup_path)
        vms[vm.name] = vm

    if format_version == 2:
        if 'dom0' in restore_info.keys() and restore_info['dom0']['good-to-go']:
            vms_dirs.append('dom0-home')
            vms_size += restore_info['dom0']['size']

        restore_vm_dirs (backup_location,
                restore_tmpdir,
                passphrase=passphrase,
                vms_dirs=vms_dirs,
                vms=vms,
                vms_size=vms_size,
                hmac_algorithm=hmac_algorithm,
                crypto_algorithm=crypto_algorithm,
                print_callback=print_callback,
                error_callback=error_callback,
                progress_callback=progress_callback,
                encrypted=encrypted,
                compressed=compressed,
                appvm=appvm)

    # Add VM in right order
    for (vm_class_name, vm_class) in sorted(QubesVmClasses.items(),
            key=lambda _x: _x[1].load_order):
        for vm in vms.values():
            if not vm.__class__ == vm_class:
                continue
            print_callback("-> Restoring {type} {0}...".format(vm.name, type=vm_class_name))
            retcode = subprocess.call (["mkdir", "-p", os.path.dirname(vm.dir_path)])
            if retcode != 0:
                error_callback("*** Cannot create directory: {0}?!".format(
                    vm.dir_path))
                error_callback("Skipping...")
                continue

            template = None
            if vm.template is not None:
                template_name = restore_info[vm.name]['template']
                template = host_collection.get_vm_by_name(template_name)

            new_vm = None

            try:
                new_vm = host_collection.add_new_vm(vm_class_name, name=vm.name,
                                                   conf_file=vm.conf_file,
                                                   dir_path=vm.dir_path,
                                                   template=template,
                                                   installed_by_rpm=False)

                if format_version == 1:
                    restore_vm_dir_v1(backup_location,
                            vm.dir_path,
                            os.path.dirname(new_vm.dir_path))
                elif format_version == 2:
                    shutil.move(os.path.join(restore_tmpdir, vm.backup_path),
                            new_vm.dir_path)

                new_vm.verify_files()
            except Exception as err:
                error_callback("ERROR: {0}".format(err))
                error_callback("*** Skipping VM: {0}".format(vm.name))
                if new_vm:
                    host_collection.pop(new_vm.qid)
                continue

            try:
                new_vm.clone_attrs(vm)
            except Exception as err:
                error_callback("ERROR: {0}".format(err))
                error_callback("*** Some VM property will not be restored")

            try:
                new_vm.appmenus_create(verbose=True)
            except Exception as err:
                error_callback("ERROR during appmenu restore: {0}".format(err))
                error_callback("*** VM '{0}' will not have appmenus".format(vm.name))

    # Set network dependencies - only non-default netvm setting
    for vm in vms.values():
        host_vm = host_collection.get_vm_by_name(vm.name)
        if host_vm is None:
            # Failed/skipped VM
            continue

        if not vm.uses_default_netvm:
            if restore_info[vm.name]['netvm'] is not None:
                host_vm.netvm = host_collection.get_vm_by_name (
                    restore_info[vm.name]['netvm'])
            else:
                host_vm.netvm = None

    host_collection.save()
    if lock_obtained:
        host_collection.unlock_db()

    # ... and dom0 home as last step
    if 'dom0' in restore_info.keys() and restore_info['dom0']['good-to-go']:
        backup_path = restore_info['dom0']['subdir']
        local_user = grp.getgrnam('qubes').gr_mem[0]
        home_dir = pwd.getpwnam(local_user).pw_dir
        if format_version == 1:
            backup_dom0_home_dir = os.path.join(backup_location, backup_path)
        else:
            backup_dom0_home_dir = os.path.join(restore_tmpdir, backup_path)
        restore_home_backupdir = "home-pre-restore-{0}".format (time.strftime("%Y-%m-%d-%H%M%S"))

        print_callback("-> Restoring home of user '{0}'...".format(local_user))
        print_callback("--> Existing files/dirs backed up in '{0}' dir".format(restore_home_backupdir))
        os.mkdir(home_dir + '/' + restore_home_backupdir)
        for f in os.listdir(backup_dom0_home_dir):
            home_file = home_dir + '/' + f
            if os.path.exists(home_file):
                os.rename(home_file, home_dir + '/' + restore_home_backupdir + '/' + f)
            if format_version == 1:
                retcode = subprocess.call (["cp", "-nrp", backup_dom0_home_dir + '/' + f, home_file])
            elif format_version == 2:
                shutil.move(backup_dom0_home_dir + '/' + f, home_file)
        retcode = subprocess.call(['sudo', 'chown', '-R', local_user, home_dir])
        if retcode != 0:
            error_callback("*** Error while setting home directory owner")

    shutil.rmtree(restore_tmpdir)

# vim:sw=4:et:
