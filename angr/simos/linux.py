import os
import logging
import struct

import claripy
from cle import MetaELF
from cle.address_translator import AT
from archinfo import ArchX86, ArchAMD64, ArchARM, ArchAArch64, ArchMIPS32, ArchMIPS64, ArchPPC32, ArchPPC64

from ..tablespecs import StringTableSpec
from ..procedures import SIM_PROCEDURES as P, SIM_LIBRARIES as L
from ..state_plugins import SimFilesystem, SimHostFilesystem
from ..storage.file import SimFile, SimFileBase
from ..errors import AngrSyscallError
from .userland import SimUserland

_l = logging.getLogger('angr.simos.linux')


class SimLinux(SimUserland):
    """
    OS-specific configuration for \\*nix-y OSes.
    """

    def __init__(self, project, **kwargs):
        super(SimLinux, self).__init__(project,
                syscall_library=L['linux'],
                syscall_addr_alignment=project.arch.instruction_alignment,
                name="Linux",
                **kwargs)

        self._loader_addr = None
        self._loader_lock_addr = None
        self._loader_unlock_addr = None
        self._error_catch_tsd_addr = None
        self._vsyscall_addr = None

    def configure_project(self): # pylint: disable=arguments-differ
        self._loader_addr = self.project.loader.extern_object.allocate()
        self._loader_lock_addr = self.project.loader.extern_object.allocate()
        self._loader_unlock_addr = self.project.loader.extern_object.allocate()
        self._error_catch_tsd_addr = self.project.loader.extern_object.allocate()
        self._vsyscall_addr = self.project.loader.extern_object.allocate()
        self.project.hook(self._loader_addr, P['linux_loader']['LinuxLoader']())
        self.project.hook(self._loader_lock_addr, P['linux_loader']['_dl_rtld_lock_recursive']())
        self.project.hook(self._loader_unlock_addr, P['linux_loader']['_dl_rtld_unlock_recursive']())
        self.project.hook(self._error_catch_tsd_addr,
                          P['linux_loader']['_dl_initial_error_catch_tsd'](
                              static_addr=self.project.loader.extern_object.allocate()
                          )
                          )
        self.project.hook(self._vsyscall_addr, P['linux_kernel']['_vsyscall']())

        ld_obj = self.project.loader.linux_loader_object
        if ld_obj is not None:
            # there are some functions we MUST use the simprocedures for, regardless of what the user wants
            self._weak_hook_symbol('__tls_get_addr', L['ld.so'].get('__tls_get_addr', self.arch), ld_obj)
            self._weak_hook_symbol('___tls_get_addr', L['ld.so'].get('___tls_get_addr', self.arch), ld_obj)

            # set up some static data in the loader object...
            _rtld_global = ld_obj.get_symbol('_rtld_global')
            if _rtld_global is not None:
                if isinstance(self.project.arch, ArchAMD64):
                    self.project.loader.memory.pack_word(_rtld_global.rebased_addr + 0xF08, self._loader_lock_addr)
                    self.project.loader.memory.pack_word(_rtld_global.rebased_addr + 0xF10, self._loader_unlock_addr)
                    self.project.loader.memory.pack_word(_rtld_global.rebased_addr + 0x990, self._error_catch_tsd_addr)

            # TODO: what the hell is this
            _rtld_global_ro = ld_obj.get_symbol('_rtld_global_ro')
            if _rtld_global_ro is not None:
                pass

        libc_obj = self.project.loader.find_object('libc.so.6')
        if libc_obj:
            self._weak_hook_symbol('_dl_vdso_vsym', L['libc.so.6'].get('_dl_vdso_vsym', self.arch), libc_obj)

        tls_obj = self.project.loader.tls_object
        if tls_obj is not None:
            if isinstance(self.project.arch, ArchAMD64):
                self.project.loader.memory.pack_word(tls_obj.thread_pointer + 0x28, 0x5f43414e4152595f)
                self.project.loader.memory.pack_word(tls_obj.thread_pointer + 0x30, 0x5054524755415244)
            elif isinstance(self.project.arch, ArchX86):
                self.project.loader.memory.pack_word(tls_obj.thread_pointer + 0x10, self._vsyscall_addr)
            elif isinstance(self.project.arch, ArchARM):
                self.project.hook(0xffff0fe0, P['linux_kernel']['_kernel_user_helper_get_tls']())

        # Only set up ifunc resolution if we are using the ELF backend on AMD64
        if isinstance(self.project.loader.main_object, MetaELF):
            if isinstance(self.project.arch, (ArchAMD64, ArchX86)):
                for binary in self.project.loader.all_objects:
                    if not isinstance(binary, MetaELF):
                        continue
                    for reloc in binary.relocs:
                        if reloc.symbol is None or reloc.resolvedby is None:
                            continue
                        try:
                            if reloc.resolvedby.elftype != 'STT_GNU_IFUNC':
                                continue
                        except AttributeError:
                            continue
                        gotaddr = reloc.rebased_addr
                        gotvalue = self.project.loader.memory.unpack_word(gotaddr)
                        if self.project.is_hooked(gotvalue):
                            continue
                        # Replace it with a ifunc-resolve simprocedure!
                        kwargs = {
                            'funcaddr': gotvalue,
                            'gotaddr': gotaddr,
                            'funcname': reloc.symbol.name
                        }
                        # TODO: should this be replaced with hook_symbol?
                        randaddr = self.project.loader.extern_object.allocate()
                        self.project.hook(randaddr, P['linux_loader']['IFuncResolver'](**kwargs))
                        self.project.loader.memory.pack_word(gotaddr, randaddr)

        # maybe move this into archinfo?
        if self.arch.name == 'X86':
            syscall_abis = ['i386']
        elif self.arch.name == 'AMD64':
            syscall_abis = ['i386', 'amd64']
        elif self.arch.name.startswith('ARM'):
            syscall_abis = ['arm']
            if self.arch.name == 'ARMHF':
                syscall_abis.append('armhf')
        elif self.arch.name == 'AARCH64':
            syscall_abis = ['aarch64']
        # https://www.linux-mips.org/wiki/WhatsWrongWithO32N32N64
        elif self.arch.name == 'MIPS32':
            syscall_abis = ['mips-o32']
        elif self.arch.name == 'MIPS64':
            syscall_abis = ['mips-n32', 'mips-n64']
        elif self.arch.name == 'PPC32':
            syscall_abis = ['ppc']
        elif self.arch.name == 'PPC64':
            syscall_abis = ['ppc64']
        else:
            syscall_abis = [] # ?

        super(SimLinux, self).configure_project(syscall_abis)

    def syscall_abi(self, state):
        if state.arch.name != 'AMD64':
            return None
        if state.history.jumpkind == 'Ijk_Sys_int128':
            return 'i386'
        elif state.history.jumpkind == 'Ijk_Sys_syscall':
            return 'amd64'
        else:
            raise AngrSyscallError("Unknown syscall jumpkind %s" % state.history.jumpkind)

    # pylint: disable=arguments-differ
    def state_blank(self, fs=None, concrete_fs=False, chroot=None,
            cwd=b'/home/user', pathsep=b'/', **kwargs):
        state = super(SimLinux, self).state_blank(**kwargs)

        if self.project.loader.tls_object is not None:
            if isinstance(state.arch, ArchAMD64):
                state.regs.fs = self.project.loader.tls_object.user_thread_pointer
            elif isinstance(state.arch, ArchX86):
                state.regs.gs = self.project.loader.tls_object.user_thread_pointer >> 16
            elif isinstance(state.arch, (ArchMIPS32, ArchMIPS64)):
                state.regs.ulr = self.project.loader.tls_object.user_thread_pointer
            elif isinstance(state.arch, ArchPPC32):
                state.regs.r2 = self.project.loader.tls_object.user_thread_pointer
            elif isinstance(state.arch, ArchPPC64):
                state.regs.r13 = self.project.loader.tls_object.user_thread_pointer
            elif isinstance(state.arch, ArchAArch64):
                state.regs.tpidr_el0 = self.project.loader.tls_object.user_thread_pointer


        if fs is None: fs = {}
        for name in fs:
            if type(fs[name]) is str:
                fs[name] = fs[name].encode('utf-8')
            if type(fs[name]) is bytes:
                fs[name] = claripy.BVV(fs[name])
            if isinstance(fs[name], claripy.Bits):
                fs[name] = SimFile(name, content=fs[name])
            if not isinstance(fs[name], SimFileBase):
                raise TypeError("Provided fs initializer with unusable type %r" % type(fs[name]))

        mounts = {}
        if concrete_fs:
            mounts[pathsep] = SimHostFilesystem(chroot if chroot is not None else os.path.sep)

        state.register_plugin('fs', SimFilesystem(files=fs, pathsep=pathsep, cwd=cwd, mountpoints=mounts))

        if self.project.loader.main_object.is_ppc64_abiv1:
            state.libc.ppc64_abiv = 'ppc64_1'

        return state

    def state_entry(self, args=None, env=None, argc=None, **kwargs):
        state = super(SimLinux, self).state_entry(**kwargs)

        # Handle default values
        filename = self.project.filename or 'dummy_filename'
        if args is None:
            args = [filename]

        if env is None:
            env = {}

        # Prepare argc
        if argc is None:
            argc = claripy.BVV(len(args), state.arch.bits)
        elif type(argc) is int:  # pylint: disable=unidiomatic-typecheck
            argc = claripy.BVV(argc, state.arch.bits)

        # Make string table for args/env/auxv
        table = StringTableSpec()

        # Add args to string table
        table.append_args(args)

        # Add environment to string table
        table.append_env(env)

        # Prepare the auxiliary vector and add it to the end of the string table
        # TODO: Actually construct a real auxiliary vector
        # current vector is an AT_RANDOM entry where the "random" value is 0xaec0aec0aec0...
        aux = [(25, b"\xAE\xC0" * 8)]
        for a, b in aux:
            table.add_pointer(a)
            if isinstance(b, bytes):
                table.add_string(b)
            else:
                table.add_pointer(b)

        table.add_null()
        table.add_null()

        # Dump the table onto the stack, calculate pointers to args, env, and auxv
        state.memory.store(state.regs.sp - 16, claripy.BVV(0, 8 * 16))
        argv = table.dump(state, state.regs.sp - 16)
        envp = argv + ((len(args) + 1) * state.arch.bytes)
        auxv = argv + ((len(args) + len(env) + 2) * state.arch.bytes)

        # Put argc on stack and fix the stack pointer
        newsp = argv - state.arch.bytes
        state.memory.store(newsp, argc, endness=state.arch.memory_endness)
        state.regs.sp = newsp

        if state.arch.name in ('PPC32',):
            state.stack_push(claripy.BVV(0, 32))
            state.stack_push(claripy.BVV(0, 32))
            state.stack_push(claripy.BVV(0, 32))
            state.stack_push(claripy.BVV(0, 32))

        # store argc argv envp auxv in the posix plugin
        state.posix.argv = argv
        state.posix.argc = argc
        state.posix.environ = envp
        state.posix.auxv = auxv
        self.set_entry_register_values(state)

        return state

    def set_entry_register_values(self, state):
        for reg, val in state.arch.entry_register_values.items():
            if isinstance(val, int):
                state.registers.store(reg, val, size=state.arch.bytes)
            elif isinstance(val, (str,)):
                if val == 'argc':
                    state.registers.store(reg, state.posix.argc, size=state.arch.bytes)
                elif val == 'argv':
                    state.registers.store(reg, state.posix.argv)
                elif val == 'envp':
                    state.registers.store(reg, state.posix.environ)
                elif val == 'auxv':
                    state.registers.store(reg, state.posix.auxv)
                elif val == 'ld_destructor':
                    # a pointer to the dynamic linker's destructor routine, to be called at exit
                    # or NULL. We like NULL. It makes things easier.
                    state.registers.store(reg, 0)
                elif val == 'toc':
                    if self.project.loader.main_object.is_ppc64_abiv1:
                        state.registers.store(reg, self.project.loader.main_object.ppc64_initial_rtoc)
                elif val == 'thread_pointer':
                    state.registers.store(reg, self.project.loader.tls_object.user_thread_pointer)
                else:
                    _l.warning('Unknown entry point register value indicator "%s"', val)
            else:
                _l.error('What the ass kind of default value is %s?', val)

    def state_full_init(self, **kwargs):
        kwargs['addr'] = self._loader_addr
        return super(SimLinux, self).state_full_init(**kwargs)

    def prepare_function_symbol(self, symbol_name, basic_addr=None):
        """
        Prepare the address space with the data necessary to perform relocations pointing to the given symbol.

        Returns a 2-tuple. The first item is the address of the function code, the second is the address of the
        relocation target.
        """
        if self.project.loader.main_object.is_ppc64_abiv1:
            if basic_addr is not None:
                pointer = self.project.loader.memory.unpack_word(basic_addr)
                return pointer, basic_addr

            pseudo_hookaddr = self.project.loader.extern_object.get_pseudo_addr(symbol_name)
            pseudo_toc = self.project.loader.extern_object.allocate(size=0x18)
            self.project.loader.extern_object.memory.pack_word(
                AT.from_mva(pseudo_toc, self.project.loader.extern_object).to_rva(), pseudo_hookaddr)
            return pseudo_hookaddr, pseudo_toc
        else:
            if basic_addr is None:
                basic_addr = self.project.loader.extern_object.get_pseudo_addr(symbol_name)
            return basic_addr, basic_addr

    def initialize_segment_register_x64(self, state, concrete_target):
        """
        Set the fs register in the angr to the value of the fs register in the concrete process

        :param state:               state which will be modified
        :param concrete_target:     concrete target that will be used to read the fs register
        :return:
        """
        _l.debug("Synchronizing fs segment register")
        state.regs.fs = self._read_fs_register_x64(concrete_target)

    def initialize_gdt_x86(self,state,concrete_target):
        """
        Create a GDT in the state memory and populate the segment registers.
        Rehook the vsyscall address using the real value in the concrete process memory

        :param state:               state which will be modified
        :param concrete_target:     concrete target that will be used to read the fs register
        :return:
        """
        _l.debug("Creating fake Global Descriptor Table and synchronizing gs segment register")
        gs = self._read_gs_register_x86(concrete_target)
        gdt = self.generate_gdt(0x0, gs)
        self.setup_gdt(state, gdt)

        # Synchronize the address of vsyscall in simprocedures dictionary with the concrete value
        _vsyscall_address = concrete_target.read_memory(gs + 0x10, state.project.arch.bits / 8)
        _vsyscall_address = struct.unpack(state.project.arch.struct_fmt(), _vsyscall_address)[0]
        state.project.rehook_symbol(_vsyscall_address, '_vsyscall')

        return gdt

    @staticmethod
    def _read_fs_register_x64(concrete_target):
        '''
        Injects a small shellcode to leak the fs segment register address. In Linux x64 this address is pointed by fs[0]
        :param concrete_target: ConcreteTarget which will be used to get the fs register address
        :return: fs register address
        :rtype string
        '''
        # register used to read the value of the segment register
        exfiltration_reg = "rax"
        # instruction to inject for reading the value at segment value = offset
        read_fs0_x64 = b"\x64\x48\x8B\x04\x25\x00\x00\x00\x00\x90\x90\x90\x90"  # mov rax, fs:[0]

        return concrete_target.execute_shellcode(read_fs0_x64, exfiltration_reg)

    @staticmethod
    def _read_gs_register_x86(concrete_target):
        '''
        Injects a small shellcode to leak the gs segment register address. In Linux x86 this address is pointed by gs[0]
        :param concrete_target: ConcreteTarget which will be used to get the gs register address
        :return: gs register address
        :rtype :str
        '''
        # register used to read the value of the segment register
        exfiltration_reg = "eax"
        # instruction to inject for reading the value at segment value = offset
        read_gs0_x64 = b"\x65\xA1\x00\x00\x00\x00\x90\x90\x90\x90"  # mov eax, gs:[0]
        return concrete_target.execute_shellcode(read_gs0_x64, exfiltration_reg)

    def get_segment_register_name(self):
        if isinstance(self.arch, ArchAMD64):
            for register in self.arch.register_list:
                if register.name == 'fs':
                    return register.vex_offset
        elif isinstance(self.arch, ArchX86):
            for register in self.arch.register_list:
                if register.name == 'gs':
                    return register.vex_offset

    def get_binary_header_name(self):
        return "ELF"
