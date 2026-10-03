Name: hello
Version: 2.12
Release: 1%{?dist}
Summary: Hello prints a greeting
License: GPL-3.0-or-later
URL: https://www.gnu.org/software/hello/
Source0: https://ftp.gnu.org/gnu/hello/hello-%{version}.tar.gz
BuildRequires: gcc, make
Requires: glibc
%description
Hello prints a greeting.
%package utils
Summary: Hello utilities
%description utils
Extra utilities.
%prep
%autosetup
%build
%configure
make %{?_smp_mflags}
%install
%make_install
%files
%{_bindir}/hello
%config(noreplace) %{_sysconfdir}/hello.conf
%post
echo installed
%files utils
%{_bindir}/hello-utils
